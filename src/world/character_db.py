"""Minimal character storage for the 3.3.5a character-selection screen."""
from __future__ import annotations

import json
import re
import sqlite3
import struct
import zlib
from typing import Dict, List, Optional, Tuple

from src.world import databases

CREATE_CHARACTERS_SQL = """
CREATE TABLE IF NOT EXISTS characters(
  guid INTEGER PRIMARY KEY AUTOINCREMENT,
  account_id INTEGER NOT NULL,
  name TEXT NOT NULL UNIQUE COLLATE NOCASE,
  race INTEGER NOT NULL DEFAULT 1,
  class INTEGER NOT NULL DEFAULT 1,
  gender INTEGER NOT NULL DEFAULT 0,
  skin INTEGER NOT NULL DEFAULT 0,
  face INTEGER NOT NULL DEFAULT 0,
  hair_style INTEGER NOT NULL DEFAULT 0,
  hair_color INTEGER NOT NULL DEFAULT 0,
  facial_style INTEGER NOT NULL DEFAULT 0,
  level INTEGER NOT NULL DEFAULT 1,
  xp INTEGER NOT NULL DEFAULT 0,
  money INTEGER NOT NULL DEFAULT 0,
  zone INTEGER NOT NULL DEFAULT 12,
  map INTEGER NOT NULL DEFAULT 0,
  x REAL NOT NULL DEFAULT -8949.95,
  y REAL NOT NULL DEFAULT -132.493,
  z REAL NOT NULL DEFAULT 83.5312,
  o REAL NOT NULL DEFAULT 0.0,
  guild_id INTEGER NOT NULL DEFAULT 0,
  char_flags INTEGER NOT NULL DEFAULT 0,
  customize_flags INTEGER NOT NULL DEFAULT 0,
  first_login INTEGER NOT NULL DEFAULT 1,
  pet_display_id INTEGER NOT NULL DEFAULT 0,
  pet_level INTEGER NOT NULL DEFAULT 0,
  pet_family INTEGER NOT NULL DEFAULT 0,
  equipment TEXT NOT NULL DEFAULT '',
  bytes1 INTEGER NOT NULL DEFAULT 0,
  bytes2 INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_characters_account ON characters(account_id);

-- Item templates: static definitions for items that can exist in the world.
-- Fields mirror the client's ItemStats_C layout for SMSG_ITEM_QUERY_SINGLE_RESPONSE.
CREATE TABLE IF NOT EXISTS item_templates(
  entry INTEGER PRIMARY KEY,
  class INTEGER NOT NULL DEFAULT 0,
  subclass INTEGER NOT NULL DEFAULT 0,
  sound_override_subclass INTEGER NOT NULL DEFAULT 0,
  name1 TEXT NOT NULL DEFAULT '',
  name2 TEXT NOT NULL DEFAULT '',
  name3 TEXT NOT NULL DEFAULT '',
  name4 TEXT NOT NULL DEFAULT '',
  display_id INTEGER NOT NULL DEFAULT 0,
  quality INTEGER NOT NULL DEFAULT 0,
  flags1 INTEGER NOT NULL DEFAULT 0,
  flags2 INTEGER NOT NULL DEFAULT 0,
  buy_price INTEGER NOT NULL DEFAULT 0,
  sell_price INTEGER NOT NULL DEFAULT 0,
  inventory_type INTEGER NOT NULL DEFAULT 0,
  allowable_class INTEGER NOT NULL DEFAULT 0,
  allowable_race INTEGER NOT NULL DEFAULT 0,
  item_level INTEGER NOT NULL DEFAULT 0,
  required_level INTEGER NOT NULL DEFAULT 0,
  required_skill INTEGER NOT NULL DEFAULT 0,
  required_skill_rank INTEGER NOT NULL DEFAULT 0,
  required_spell INTEGER NOT NULL DEFAULT 0,
  required_honor_rank INTEGER NOT NULL DEFAULT 0,
  required_city_rank INTEGER NOT NULL DEFAULT 0,
  required_reputation_faction INTEGER NOT NULL DEFAULT 0,
  required_reputation_rank INTEGER NOT NULL DEFAULT 0,
  max_count INTEGER NOT NULL DEFAULT 0,
  stackable INTEGER NOT NULL DEFAULT 1,
  container_slots INTEGER NOT NULL DEFAULT 0,
  stats_count INTEGER NOT NULL DEFAULT 0,
  stat_type1 INTEGER NOT NULL DEFAULT 0,
  stat_value1 INTEGER NOT NULL DEFAULT 0,
  stat_type2 INTEGER NOT NULL DEFAULT 0,
  stat_value2 INTEGER NOT NULL DEFAULT 0,
  stat_type3 INTEGER NOT NULL DEFAULT 0,
  stat_value3 INTEGER NOT NULL DEFAULT 0,
  stat_type4 INTEGER NOT NULL DEFAULT 0,
  stat_value4 INTEGER NOT NULL DEFAULT 0,
  stat_type5 INTEGER NOT NULL DEFAULT 0,
  stat_value5 INTEGER NOT NULL DEFAULT 0,
  stat_type6 INTEGER NOT NULL DEFAULT 0,
  stat_value6 INTEGER NOT NULL DEFAULT 0,
  stat_type7 INTEGER NOT NULL DEFAULT 0,
  stat_value7 INTEGER NOT NULL DEFAULT 0,
  stat_type8 INTEGER NOT NULL DEFAULT 0,
  stat_value8 INTEGER NOT NULL DEFAULT 0,
  stat_type9 INTEGER NOT NULL DEFAULT 0,
  stat_value9 INTEGER NOT NULL DEFAULT 0,
  stat_type10 INTEGER NOT NULL DEFAULT 0,
  stat_value10 INTEGER NOT NULL DEFAULT 0,
  scaling_stat_distribution INTEGER NOT NULL DEFAULT 0,
  scaling_stat_value INTEGER NOT NULL DEFAULT 0,
  damage_min1 REAL NOT NULL DEFAULT 0.0,
  damage_max1 REAL NOT NULL DEFAULT 0.0,
  damage_type1 INTEGER NOT NULL DEFAULT 0,
  damage_min2 REAL NOT NULL DEFAULT 0.0,
  damage_max2 REAL NOT NULL DEFAULT 0.0,
  damage_type2 INTEGER NOT NULL DEFAULT 0,
  resistance_holy INTEGER NOT NULL DEFAULT 0,
  resistance_fire INTEGER NOT NULL DEFAULT 0,
  resistance_nature INTEGER NOT NULL DEFAULT 0,
  resistance_frost INTEGER NOT NULL DEFAULT 0,
  resistance_shadow INTEGER NOT NULL DEFAULT 0,
  resistance_arcane INTEGER NOT NULL DEFAULT 0,
  resistance_armor INTEGER NOT NULL DEFAULT 0,
  delay INTEGER NOT NULL DEFAULT 0,
  ammo_type INTEGER NOT NULL DEFAULT 0,
  ranged_mod_range REAL NOT NULL DEFAULT 0.0,
  spell_id1 INTEGER NOT NULL DEFAULT 0,
  spell_trigger1 INTEGER NOT NULL DEFAULT 0,
  spell_charges1 INTEGER NOT NULL DEFAULT 0,
  spell_cooldown1 INTEGER NOT NULL DEFAULT 0,
  spell_category1 INTEGER NOT NULL DEFAULT 0,
  spell_category_cooldown1 INTEGER NOT NULL DEFAULT 0,
  spell_ppm_rate1 REAL NOT NULL DEFAULT 0.0,
  spell_id2 INTEGER NOT NULL DEFAULT 0,
  spell_trigger2 INTEGER NOT NULL DEFAULT 0,
  spell_charges2 INTEGER NOT NULL DEFAULT 0,
  spell_cooldown2 INTEGER NOT NULL DEFAULT 0,
  spell_category2 INTEGER NOT NULL DEFAULT 0,
  spell_category_cooldown2 INTEGER NOT NULL DEFAULT 0,
  spell_ppm_rate2 REAL NOT NULL DEFAULT 0.0,
  spell_id3 INTEGER NOT NULL DEFAULT 0,
  spell_trigger3 INTEGER NOT NULL DEFAULT 0,
  spell_charges3 INTEGER NOT NULL DEFAULT 0,
  spell_cooldown3 INTEGER NOT NULL DEFAULT 0,
  spell_category3 INTEGER NOT NULL DEFAULT 0,
  spell_category_cooldown3 INTEGER NOT NULL DEFAULT 0,
  spell_ppm_rate3 REAL NOT NULL DEFAULT 0.0,
  spell_id4 INTEGER NOT NULL DEFAULT 0,
  spell_trigger4 INTEGER NOT NULL DEFAULT 0,
  spell_charges4 INTEGER NOT NULL DEFAULT 0,
  spell_cooldown4 INTEGER NOT NULL DEFAULT 0,
  spell_category4 INTEGER NOT NULL DEFAULT 0,
  spell_category_cooldown4 INTEGER NOT NULL DEFAULT 0,
  spell_ppm_rate4 REAL NOT NULL DEFAULT 0.0,
  spell_id5 INTEGER NOT NULL DEFAULT 0,
  spell_trigger5 INTEGER NOT NULL DEFAULT 0,
  spell_charges5 INTEGER NOT NULL DEFAULT 0,
  spell_cooldown5 INTEGER NOT NULL DEFAULT 0,
  spell_category5 INTEGER NOT NULL DEFAULT 0,
  spell_category_cooldown5 INTEGER NOT NULL DEFAULT 0,
  spell_ppm_rate5 REAL NOT NULL DEFAULT 0.0,
  bonding INTEGER NOT NULL DEFAULT 0,
  description TEXT NOT NULL DEFAULT '',
  page_text INTEGER NOT NULL DEFAULT 0,
  language_id INTEGER NOT NULL DEFAULT 0,
  page_material INTEGER NOT NULL DEFAULT 0,
  start_quest INTEGER NOT NULL DEFAULT 0,
  lock_id INTEGER NOT NULL DEFAULT 0,
  material INTEGER NOT NULL DEFAULT 0,
  sheath_type INTEGER NOT NULL DEFAULT 0,
  buy_count INTEGER NOT NULL DEFAULT 1,
  random_property INTEGER NOT NULL DEFAULT 0,
  random_suffix INTEGER NOT NULL DEFAULT 0,
  block INTEGER NOT NULL DEFAULT 0,
  item_set INTEGER NOT NULL DEFAULT 0,
  max_durability INTEGER NOT NULL DEFAULT 0,
  area INTEGER NOT NULL DEFAULT 0,
  map INTEGER NOT NULL DEFAULT 0,
  bag_family INTEGER NOT NULL DEFAULT 0,
  totem_category INTEGER NOT NULL DEFAULT 0,
  socket_color_1 INTEGER NOT NULL DEFAULT 0,
  socket_content_1 INTEGER NOT NULL DEFAULT 0,
  socket_color_2 INTEGER NOT NULL DEFAULT 0,
  socket_content_2 INTEGER NOT NULL DEFAULT 0,
  socket_color_3 INTEGER NOT NULL DEFAULT 0,
  socket_content_3 INTEGER NOT NULL DEFAULT 0,
  socket_bonus INTEGER NOT NULL DEFAULT 0,
  gem_properties INTEGER NOT NULL DEFAULT 0,
  required_disenchant_skill INTEGER NOT NULL DEFAULT 0,
  armor_damage_modifier REAL NOT NULL DEFAULT 0.0,
  duration INTEGER NOT NULL DEFAULT 0,
  item_limit_category INTEGER NOT NULL DEFAULT 0,
  holiday_id INTEGER NOT NULL DEFAULT 0
);

-- Character inventory: one row per item instance owned by a character.
-- slot encodes both location (equipment/bag) and position within that location.
-- Equipment slots: 0..22 (19 equip + 4 bag slots, matching INVSLOT_* constants).
-- Backpack slots: 256..271 (256 + 0..15 for 16 backpack positions).
CREATE TABLE IF NOT EXISTS character_items(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  character_guid INTEGER NOT NULL,
  item_entry INTEGER NOT NULL REFERENCES item_templates(entry),
  slot INTEGER NOT NULL DEFAULT 0,
  stack_count INTEGER NOT NULL DEFAULT 1,
  duration INTEGER NOT NULL DEFAULT 0,
  durability INTEGER NOT NULL DEFAULT 0,
  max_durability INTEGER NOT NULL DEFAULT 0,
  creator_guid INTEGER NOT NULL DEFAULT 0,
  gift_creator_guid INTEGER NOT NULL DEFAULT 0,
  flags INTEGER NOT NULL DEFAULT 0,
  enchantment_1 INTEGER NOT NULL DEFAULT 0,
  enchantment_2 INTEGER NOT NULL DEFAULT 0,
  enchantment_3 INTEGER NOT NULL DEFAULT 0,
  enchantment_4 INTEGER NOT NULL DEFAULT 0,
  enchantment_5 INTEGER NOT NULL DEFAULT 0,
  enchantment_6 INTEGER NOT NULL DEFAULT 0,
  enchantment_7 INTEGER NOT NULL DEFAULT 0,
  enchantment_8 INTEGER NOT NULL DEFAULT 0,
  enchantment_9 INTEGER NOT NULL DEFAULT 0,
  enchantment_10 INTEGER NOT NULL DEFAULT 0,
  enchantment_11 INTEGER NOT NULL DEFAULT 0,
  enchantment_12 INTEGER NOT NULL DEFAULT 0,
  property_seed INTEGER NOT NULL DEFAULT 0,
  random_properties_id INTEGER NOT NULL DEFAULT 0,
  create_played_time INTEGER NOT NULL DEFAULT 0,
  UNIQUE(character_guid, slot)
);
CREATE INDEX IF NOT EXISTS idx_character_items_character ON character_items(character_guid);

-- Character spells: one row per spell known by a character.
CREATE TABLE IF NOT EXISTS character_spells(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  character_guid INTEGER NOT NULL,
  spell INTEGER NOT NULL,
  active INTEGER NOT NULL DEFAULT 1,
  disabled INTEGER NOT NULL DEFAULT 0,
  UNIQUE(character_guid, spell)
);
CREATE INDEX IF NOT EXISTS idx_character_spells_character ON character_spells(character_guid);

-- Player per-class/per-level base HP/mana from CMaNGOS player_classlevelstats.
-- Used for correct starting mana/HP and level-up calculations.
CREATE TABLE IF NOT EXISTS player_classlevelstats(
  class INTEGER NOT NULL,
  level INTEGER NOT NULL,
  basehp INTEGER NOT NULL DEFAULT 0,
  basemana INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (class, level)
);
CREATE INDEX IF NOT EXISTS idx_player_classlevelstats_class ON player_classlevelstats(class);

-- Player per-race/per-class/per-level base stats from CMaNGOS player_levelstats.
-- These are the values the client shows in the character sheet (strength,
-- agility, stamina, intellect, spirit) before any item bonuses.
CREATE TABLE IF NOT EXISTS player_levelstats(
  race INTEGER NOT NULL,
  class INTEGER NOT NULL,
  level INTEGER NOT NULL,
  str INTEGER NOT NULL DEFAULT 0,
  agi INTEGER NOT NULL DEFAULT 0,
  sta INTEGER NOT NULL DEFAULT 0,
  inte INTEGER NOT NULL DEFAULT 0,
  spi INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (race, class, level)
);
CREATE INDEX IF NOT EXISTS idx_player_levelstats_race_class ON player_levelstats(race, class);
"""


def _schema_subset(sql: str, tables: set[str]) -> str:
    """Extract table/index statements for the named tables."""
    statements = []
    for statement in sql.split(";"):
        text = statement.strip()
        if not text:
            continue
        if re.search(r"\bINDEX\b", text[:80], re.I):
            match = re.search(r"\bON\s+([A-Za-z_][A-Za-z0-9_]*)", text,
                              re.I)
            name = match.group(1) if match else ""
        else:
            match = re.search(
                r"\bTABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?([A-Za-z_][A-Za-z0-9_]*)",
                text, re.I)
            name = match.group(1) if match else ""
        if name.lower() in tables:
            statements.append(text)
    return ";\n".join(statements) + ";"


# Character data is stored in its own SQLite file.  The combined schema is
# retained for tests/tools that explicitly create a self-contained database.
CREATE_CHARACTER_TABLES_SQL = _schema_subset(
    CREATE_CHARACTERS_SQL, {"characters", "character_items",
                            "character_spells"})
CREATE_WORLD_TEMPLATE_SQL = _schema_subset(
    CREATE_CHARACTERS_SQL, {"item_templates", "player_classlevelstats",
                            "player_levelstats"})

WORLD_VIEW_TABLES = databases.CHARACTER_WORLD_VIEWS


def configure(world_path: str, character_path: Optional[str] = None) -> None:
    """Route character-database calls to a separate file when configured."""
    databases.configure(world_path, character_path)


def _same_path(first: str, second: str) -> bool:
    return databases.same_path(first, second)


def _connect(path: str) -> sqlite3.Connection:
    """Open the database that owns characters, attaching the world bank."""
    return databases.connect_character(path)


def _open_raw(path: str) -> sqlite3.Connection:
    """Open ``path`` directly, bypassing the split-database routing."""
    return sqlite3.connect(path)


def _migrate_item_template_columns(con: sqlite3.Connection) -> None:
    """Add item-template columns introduced after the first schema revision."""
    columns = {row[1] for row in con.execute(
        "PRAGMA table_info(item_templates)")}
    for index in range(1, 6):
        column = f"spell_ppm_rate{index}"
        if column not in columns:
            con.execute(
                f"ALTER TABLE item_templates ADD COLUMN {column}"
                " REAL NOT NULL DEFAULT 0.0")
    if "start_quest" not in columns:
        # item_template.startquest: using the item opens that quest.
        con.execute(
            "ALTER TABLE item_templates ADD COLUMN start_quest INTEGER"
            " NOT NULL DEFAULT 0")
    if "buy_count" not in columns:
        # Vendors sell some items in fixed bundles (water x5, arrows x200).
        con.execute(
            "ALTER TABLE item_templates ADD COLUMN buy_count INTEGER"
            " NOT NULL DEFAULT 1")


def init_world_db(path: str) -> None:
    """Create/upgrade the world-side template tables used by characters."""
    con = _open_raw(path)  # world bank: never routed
    try:
        con.executescript(CREATE_WORLD_TEMPLATE_SQL)
        _migrate_item_template_columns(con)
        _seed_starter_items(con)
        con.commit()
    finally:
        con.close()


# Tables that move from the legacy combined world database into characters.db.
CHARACTER_TABLES = (
    "characters",
    "character_items",
    "character_spells",
    "character_quests",
    "character_quests_rewarded",
)


def _table_names(con: sqlite3.Connection, schema: str = "main") -> set[str]:
    return {
        str(row[0])
        for row in con.execute(
            f"SELECT name FROM {schema}.sqlite_master WHERE type = 'table'")
    }


def _migrate_character_columns(con: sqlite3.Connection) -> None:
    """Add character columns introduced after the first schema revision."""
    columns = {row[1] for row in con.execute("PRAGMA table_info(characters)")}
    additions = {
        "o": "REAL NOT NULL DEFAULT 0.0",
        "bytes1": "INTEGER NOT NULL DEFAULT 0",
        "bytes2": "INTEGER NOT NULL DEFAULT 0",
        "xp": "INTEGER NOT NULL DEFAULT 0",
        "money": "INTEGER NOT NULL DEFAULT 0",
    }
    for name, declaration in additions.items():
        if name not in columns:
            con.execute(
                f"ALTER TABLE characters ADD COLUMN {name} {declaration}")


def init_character_store(path: str) -> None:
    """Create/upgrade the per-character database schema."""
    con = _open_raw(path)
    try:
        con.executescript(CREATE_CHARACTER_TABLES_SQL)
        _migrate_character_columns(con)
        _migrate_start_positions(con)
        con.commit()
    finally:
        con.close()


def _backfill_split_store(character_path: str, world_path: str) -> None:
    """Grant missing starter spells using the attached world bank."""
    con = _open_raw(character_path)
    try:
        con.execute("ATTACH DATABASE ? AS world", (world_path,))
        con.execute(
            "CREATE TEMP VIEW IF NOT EXISTS playercreateinfo_spell AS"
            " SELECT * FROM world.playercreateinfo_spell")
        _backfill_starter_spells(con, seed_world=False)
        con.commit()
    finally:
        con.close()


def migrate_legacy_characters(world_path: str,
                              character_path: str) -> int:
    """Copy character rows out of a pre-split combined ``world.db``.

    The function only runs into an empty character store, so it is safe to call
    on every startup: an existing ``characters.db`` is never merged with or
    overwritten by stale copies left in an old world database.  Returns the
    number of character rows copied.
    """
    world = _open_raw(world_path)
    try:
        if "characters" not in _table_names(world):
            return 0
        legacy_count = int(world.execute(
            "SELECT COUNT(*) FROM characters").fetchone()[0] or 0)
        if not legacy_count:
            return 0
    finally:
        world.close()

    con = _open_raw(character_path)
    try:
        if "characters" not in _table_names(con):
            return 0
        existing = int(con.execute(
            "SELECT COUNT(*) FROM characters").fetchone()[0] or 0)
        if existing:
            return 0
        con.execute("ATTACH DATABASE ? AS world", (world_path,))
        copied = 0
        for table in CHARACTER_TABLES:
            if table not in _table_names(con) or table not in _table_names(
                    con, "world"):
                continue
            main_columns = [row[1] for row in con.execute(
                f"PRAGMA main.table_info({table})")]
            world_columns = {row[1] for row in con.execute(
                f"PRAGMA world.table_info({table})")}
            columns = [name for name in main_columns if name in world_columns]
            if not columns:
                continue
            column_list = ", ".join(f'"{name}"' for name in columns)
            cur = con.execute(
                f'INSERT OR IGNORE INTO main."{table}"({column_list})'
                f' SELECT {column_list} FROM world."{table}"')
            if table == "characters":
                copied = max(0, int(cur.rowcount or 0))
        con.commit()
        if copied:
            print(f"Migrated {copied} character(s) from the legacy world database")
        return copied
    finally:
        con.close()


def init_split_databases(world_path: str, character_path: str) -> None:
    """Create/upgrade the world bank and the separate character store."""
    init_world_db(world_path)
    init_character_store(character_path)
    # Character quest tables must exist before the legacy rows are copied.
    from src.world import quests
    quests.init_character_quest_db(character_path)
    migrate_legacy_characters(world_path, character_path)
    # Backfill after the migration so characters imported from the old
    # combined database pick up any missing starter spells immediately.
    _backfill_split_store(character_path, world_path)


_NAME_RE = re.compile(r"^[A-Za-z]{2,12}$")
# 3.3.5a writes INVENTORY_SLOT_BAG_END (23) entries: 19 equipment slots plus
# the 4 bag slots.  Sending only 19 truncates every record and the client
# silently discards the whole SMSG_CHAR_ENUM (empty character list).
_EQUIPMENT_SLOTS = 23

# Player starting positions, copied row-for-row from the 3.3.5a world database
# (``playercreateinfo``).  Keyed by (race, class) because death knights get the
# Ebon Hold start instead of their racial home; ``start_position`` falls back to
# the race's level-1 entry and finally to the human start for custom combos.
#
# These are the *real* client coordinates: race 2/8 begin in the Valley of
# Trials (-618, -4251), not at Razor Hill, and races 10/11 start on the
# Outland-era maps 530 (Eversong/Azuremyst).  Keeping map/zone in sync matters
# for SMSG_LOGIN_VERIFY_WORLD, the bind point and SMSG_INIT_WORLD_STATES.
_START_POSITIONS: Dict[Tuple[int, int], Tuple[int, int, float, float, float, float]] = {
    (1, 1): (0, 12, -8949.95, -132.493, 83.5312, 0.0),
    (1, 2): (0, 12, -8949.95, -132.493, 83.5312, 0.0),
    (1, 4): (0, 12, -8949.95, -132.493, 83.5312, 0.0),
    (1, 5): (0, 12, -8949.95, -132.493, 83.5312, 0.0),
    (1, 6): (609, 4298, 2355.84, -5664.77, 426.028, 3.65997),
    (1, 8): (0, 12, -8949.95, -132.493, 83.5312, 0.0),
    (1, 9): (0, 12, -8949.95, -132.493, 83.5312, 0.0),
    (2, 1): (1, 14, -618.518, -4251.67, 38.718, 0.0),
    (2, 3): (1, 14, -618.518, -4251.67, 38.718, 0.0),
    (2, 4): (1, 14, -618.518, -4251.67, 38.718, 0.0),
    (2, 6): (609, 4298, 2358.44, -5666.9, 426.023, 3.65997),
    (2, 7): (1, 14, -618.518, -4251.67, 38.718, 0.0),
    (2, 9): (1, 14, -618.518, -4251.67, 38.718, 0.0),
    (3, 1): (0, 1, -6240.32, 331.033, 382.758, 6.17716),
    (3, 2): (0, 1, -6240.32, 331.033, 382.758, 6.17716),
    (3, 3): (0, 1, -6240.32, 331.033, 382.758, 6.17716),
    (3, 4): (0, 1, -6240.32, 331.033, 382.758, 6.17716),
    (3, 5): (0, 1, -6240.32, 331.033, 382.758, 6.17716),
    (3, 6): (609, 4298, 2358.44, -5666.9, 426.023, 3.65997),
    (4, 1): (1, 141, 10311.3, 831.463, 1326.41, 5.48033),
    (4, 3): (1, 141, 10311.3, 831.463, 1326.41, 5.48033),
    (4, 4): (1, 141, 10311.3, 831.463, 1326.41, 5.48033),
    (4, 5): (1, 141, 10311.3, 831.463, 1326.41, 5.48033),
    (4, 6): (609, 4298, 2356.21, -5662.21, 426.026, 3.65997),
    (4, 11): (1, 141, 10311.3, 831.463, 1326.41, 5.48033),
    (5, 1): (0, 85, 1676.35, 1677.45, 121.67, 2.70526),
    (5, 4): (0, 85, 1676.35, 1677.45, 121.67, 2.70526),
    (5, 5): (0, 85, 1676.35, 1677.45, 121.67, 2.70526),
    (5, 6): (609, 4298, 2356.21, -5662.21, 426.026, 3.65997),
    (5, 8): (0, 85, 1676.35, 1677.45, 121.67, 2.70526),
    (5, 9): (0, 85, 1676.35, 1677.45, 121.67, 2.70526),
    (6, 1): (1, 215, -2917.58, -257.98, 52.9968, 0.0),
    (6, 3): (1, 215, -2917.58, -257.98, 52.9968, 0.0),
    (6, 6): (609, 4298, 2358.17, -5663.21, 426.027, 3.65997),
    (6, 7): (1, 215, -2917.58, -257.98, 52.9968, 0.0),
    (6, 11): (1, 215, -2917.58, -257.98, 52.9968, 0.0),
    (7, 1): (0, 1, -6240.32, 331.033, 382.758, 0.0),
    (7, 4): (0, 1, -6240.0, 331.0, 383.0, 0.0),
    (7, 6): (609, 4298, 2355.05, -5661.7, 426.026, 3.65997),
    (7, 8): (0, 1, -6240.0, 331.0, 383.0, 0.0),
    (7, 9): (0, 1, -6240.0, 331.0, 383.0, 0.0),
    (8, 1): (1, 14, -618.518, -4251.67, 38.718, 0.0),
    (8, 3): (1, 14, -618.518, -4251.67, 38.718, 0.0),
    (8, 4): (1, 14, -618.518, -4251.67, 38.718, 0.0),
    (8, 5): (1, 14, -618.518, -4251.67, 38.718, 0.0),
    (8, 6): (609, 4298, 2355.05, -5661.7, 426.026, 3.65997),
    (8, 7): (1, 14, -618.518, -4251.67, 38.718, 0.0),
    (8, 8): (1, 14, -618.518, -4251.67, 38.718, 0.0),
    (10, 2): (530, 3431, 10349.6, -6357.29, 33.4026, 5.31605),
    (10, 3): (530, 3431, 10349.6, -6357.29, 33.4026, 5.31605),
    (10, 4): (530, 3431, 10349.6, -6357.29, 33.4026, 5.31605),
    (10, 5): (530, 3431, 10349.6, -6357.29, 33.4026, 5.31605),
    (10, 6): (609, 4298, 2355.84, -5664.77, 426.028, 3.65997),
    (10, 8): (530, 3431, 10349.6, -6357.29, 33.4026, 5.31605),
    (10, 9): (530, 3431, 10349.6, -6357.29, 33.4026, 5.31605),
    (11, 1): (530, 3526, -3961.64, -13931.2, 100.615, 2.08364),
    (11, 2): (530, 3526, -3961.64, -13931.2, 100.615, 2.08364),
    (11, 3): (530, 3526, -3961.64, -13931.2, 100.615, 2.08364),
    (11, 5): (530, 3526, -3961.64, -13931.2, 100.615, 2.08364),
    (11, 6): (609, 4298, 2358.17, -5663.21, 426.027, 3.65997),
    (11, 7): (530, 3526, -3961.64, -13931.2, 100.615, 2.08364),
    (11, 8): (530, 3526, -3961.64, -13931.2, 100.615, 2.08364),
}

_HUMAN_START = (0, 12, -8949.95, -132.493, 83.5312, 0.0)


def start_position(race: int, char_class: int = 1) -> Tuple[int, int, float, float, float, float]:
    """Return ``(map, zone, x, y, z, o)`` for a new character.

    Death knights get their ``(race, 6)`` row; every other class uses the
    race's level-1 row.  Unknown/custom combinations fall back to the human
    start so character creation never leaves a character at the origin.
    """
    race = int(race)
    char_class = int(char_class)
    exact = _START_POSITIONS.get((race, char_class))
    if exact is not None:
        return exact
    level_one = _START_POSITIONS.get((race, 1))
    if level_one is not None:
        return level_one
    # Races without a warrior entry (blood elf) still share one home zone, so a
    # custom class combination gets that race's start instead of Northshire.
    for (candidate_race, _candidate_class), value in sorted(_START_POSITIONS.items()):
        if candidate_race == race:
            return value
    return _HUMAN_START


# Legal 3.3.5a race/class combinations, straight from the client's
# CharBaseInfo.dbc.  Character creation must reject anything not listed here:
# the client's own creation UI only offers these, but a hand-built
# CMSG_CHAR_CREATE could otherwise create impossible characters (a gnome
# paladin, an orc druid, ...) that have no start outfit and no start position.
RACE_CLASSES = {
    1: (1, 2, 4, 5, 6, 8, 9),        # Human
    2: (1, 3, 4, 6, 7, 9),           # Orc
    3: (1, 2, 3, 4, 5, 6),           # Dwarf
    4: (1, 3, 4, 5, 6, 11),          # Night Elf
    5: (1, 4, 5, 6, 8, 9),           # Undead
    6: (1, 3, 6, 7, 11),             # Tauren
    7: (1, 4, 6, 8, 9),              # Gnome
    8: (1, 3, 4, 5, 6, 7, 8),        # Troll
    10: (2, 3, 4, 5, 6, 8, 9),       # Blood Elf
    11: (1, 2, 3, 5, 6, 7, 8),       # Draenei
}


def is_valid_race_class(race: int, char_class: int) -> bool:
    """Return True when ``race`` may roll ``char_class`` in 3.3.5a."""
    return int(char_class) in RACE_CLASSES.get(int(race), ())


def init_character_db(path: str) -> None:
    """Create the combined schema in one file (tests and one-off tools).

    Production uses :func:`init_split_databases` so that characters live in
    ``data/characters.db`` while the regenerated world bank stays read-only.
    """
    con = _open_raw(path)  # init: may be the combined test database
    try:
        con.executescript(CREATE_CHARACTERS_SQL)
        _migrate_character_columns(con)
        _migrate_item_template_columns(con)
        _migrate_start_positions(con)
        _seed_starter_items(con)
        _backfill_starter_spells(con)
        con.commit()
    finally:
        con.close()


def _starter_spells_for(con: sqlite3.Connection, race: int,
                        char_class: int) -> List[int]:
    """Return the 3.3.5a starter spell ids for a race/class combination.

    ``playercreateinfo_spell`` is the authoritative list (it includes racials,
    languages, armor/weapon proficiencies and the class's level-1 abilities).
    The class-only table below is only a fallback for a database that has not
    loaded ``playercreateinfo.sql`` yet.
    """
    try:
        rows = con.execute(
            "SELECT spell FROM playercreateinfo_spell"
            " WHERE race = ? AND class = ? ORDER BY spell",
            (int(race), int(char_class)),
        ).fetchall()
    except sqlite3.Error:
        rows = []
    spells = [int(row[0]) for row in rows]
    if not spells:
        spells = list(_STARTER_SPELLS_BY_CLASS.get(int(char_class), []))
    for spell_id in _EXTRA_STARTER_SPELLS_BY_CLASS.get(int(char_class), []):
        if spell_id not in spells:
            spells.append(spell_id)
    return spells


def get_starter_spells(path: str, race: int, char_class: int) -> List[int]:
    """Public helper for callers that only have the database path."""
    con = _connect(path)
    try:
        return _starter_spells_for(con, race, char_class)
    finally:
        con.close()


def _backfill_starter_spells(con: sqlite3.Connection,
                             seed_world: bool = True) -> None:
    """Grant any missing starter spells to every character.

    Characters created before the authentic ``playercreateinfo_spell`` data
    was imported still pick up their real level-1 spellbook without a wipe.
    ``INSERT OR IGNORE`` makes the pass a no-op for spells already known.  If
    the table does not exist yet (a tool opened the character database before
    ``init_creature_db``), the backfill is skipped rather than guessing with
    the class-only fallback.

    ``seed_world`` seeds the hand-written starter item rows into the same file.
    A split character database must pass ``False``: its world templates live in
    the attached world bank.
    """
    try:
        if con.execute(
                "SELECT 1 FROM playercreateinfo_spell LIMIT 1").fetchone() is None:
            if seed_world:
                _seed_starter_items(con)
            con.commit()
            return
    except sqlite3.Error:
        if seed_world:
            _seed_starter_items(con)
        con.commit()
        return
    rows = con.execute("SELECT guid, race, class FROM characters").fetchall()
    added = 0
    for guid, race, char_class in rows:
        for spell_id in _starter_spells_for(con, int(race), int(char_class)):
            cur = con.execute(
                "INSERT OR IGNORE INTO character_spells(character_guid, spell, active) VALUES(?,?,1)",
                (int(guid), int(spell_id)),
            )
            added += max(0, int(cur.rowcount or 0))
    if added:
        con.commit()
        print(f"Backfilled {added} missing starter spell(s)")

    if seed_world:
        _seed_starter_items(con)
        con.commit()


def _migrate_start_positions(con: sqlite3.Connection) -> None:
    """Move characters created before per-race spawns to their real start.

    Older builds inserted every race at the human Northshire coordinates with
    the schema's default zone 1519 (Stormwind).  Only rows that are still at
    that exact spot are touched, so a character who has actually walked away
    keeps their position.  The old z default (83.53) differs by 0.0012 from the
    real start (83.5312), hence the tolerances below.
    """
    columns = {row[1] for row in con.execute("PRAGMA table_info(characters)")}
    if not {"race", "class", "map", "zone", "x", "y", "z", "o"}.issubset(columns):
        return
    rows = con.execute(
        "SELECT guid, race, class FROM characters"
        " WHERE zone = 1519"
        " AND ABS(x + 8949.95) < 0.01"
        " AND ABS(y + 132.49) < 0.01"
        " AND ABS(z - 83.53) < 0.01"
    ).fetchall()
    for guid, race, char_class in rows:
        map_id, zone, x, y, z, o = start_position(race, char_class)
        con.execute(
            "UPDATE characters SET map = ?, zone = ?, x = ?, y = ?, z = ?, o = ?"
            " WHERE guid = ?",
            (map_id, zone, x, y, z, o, guid),
        )


def normalize_name(name: str) -> Optional[str]:
    """WoW-style name normalization: first letter upper, rest lower."""
    name = (name or "").strip()
    if not _NAME_RE.match(name):
        return None
    return name[:1].upper() + name[1:].lower()


def list_characters(path: str, account_id: int) -> List[sqlite3.Row]:
    con = _connect(path)
    con.row_factory = sqlite3.Row
    try:
        return con.execute(
            "SELECT * FROM characters WHERE account_id = ? ORDER BY guid LIMIT 10",
            (account_id,),
        ).fetchall()
    finally:
        con.close()


def get_character(path: str, account_id: int, guid: int) -> Optional[sqlite3.Row]:
    """Return the character row if it belongs to ``account_id``."""
    con = _connect(path)
    con.row_factory = sqlite3.Row
    try:
        return con.execute(
            "SELECT * FROM characters WHERE guid = ? AND account_id = ?",
            (guid, account_id),
        ).fetchone()
    finally:
        con.close()


def get_character_by_guid(path: str, guid: int) -> Optional[sqlite3.Row]:
    """Return a character row regardless of which account owns it.

    Used to answer name/query packets for other players that this client can
    see, and to rebuild their create block when they enter the world.

    Character guids in this server are 32-bit row ids; a 64-bit object guid
    (for example a creature's ``0xF130...`` guid) must not reach SQLite, whose
    INTEGER is signed 64-bit and raises ``OverflowError`` for values >= 2**63.
    """
    guid = int(guid)
    if not (0 < guid <= 0xFFFFFFFF):
        return None
    con = _connect(path)
    con.row_factory = sqlite3.Row
    try:
        return con.execute(
            "SELECT * FROM characters WHERE guid = ?", (guid,)
        ).fetchone()
    finally:
        con.close()


def find_character_by_name(path: str, name: str) -> Optional[sqlite3.Row]:
    """Case-insensitive name lookup for whisper targets.

    The client sends the raw name the player typed, so we normalize it the same
    way we do at character creation (first letter upper, rest lower) and match
    against the stored name.  Returns the row for the character on this realm,
    or ``None`` when nobody by that name exists.
    """
    normalized = normalize_name(name)
    if normalized is None:
        return None
    con = _connect(path)
    con.row_factory = sqlite3.Row
    try:
        return con.execute(
            "SELECT * FROM characters WHERE name = ? COLLATE NOCASE", (normalized,)
        ).fetchone()
    finally:
        con.close()


# 3.3.5a death knights begin at level 55; every other class starts at 1.  The
# starting attributes and health/mana pools are looked up for this level, so a
# DK gets the real level-55 `player_levelstats`/`player_classlevelstats` rows.
_START_LEVEL = {6: 55}


def start_level(char_class: int) -> int:
    """The level a newly created character of ``char_class`` begins at."""
    return _START_LEVEL.get(int(char_class), 1)


def create_character(path: str, account_id: int, name: str, race: int, char_class: int,
                     gender: int, skin: int, face: int, hair_style: int,
                     hair_color: int, facial_style: int) -> int:
    """Create a character and return its GUID.

    Raises ``sqlite3.IntegrityError`` when the name is already taken.
    """
    map_id, zone, x, y, z, o = start_position(race, char_class)
    level = start_level(char_class)
    con = _connect(path)
    try:
        cur = con.execute(
            "INSERT INTO characters(account_id, name, race, class, gender, skin, face, "
            "hair_style, hair_color, facial_style, level, map, zone, x, y, z, o) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (account_id, name, race, char_class, gender, skin, face,
             hair_style, hair_color, facial_style, level, map_id, zone, x, y, z, o),
        )
        guid = int(cur.lastrowid)
        _grant_starter_spells(con, guid, race, char_class)
        con.commit()
        return guid
    finally:
        con.close()


def delete_character(path: str, account_id: int, guid: int) -> bool:
    con = _connect(path)
    try:
        cur = con.execute(
            "DELETE FROM characters WHERE guid = ? AND account_id = ?",
            (guid, account_id),
        )
        if cur.rowcount > 0:
            from src.world import quests
            quests.delete_character_quests(con, guid)
        con.commit()
        return cur.rowcount > 0
    finally:
        con.close()


def character_exists(path: str, account_id: int, guid: int) -> bool:
    con = _connect(path)
    try:
        row = con.execute(
            "SELECT 1 FROM characters WHERE guid = ? AND account_id = ?",
            (guid, account_id),
        ).fetchone()
        return row is not None
    finally:
        con.close()


def update_character_position(path: str, guid: int, x: float, y: float, z: float,
                              o: float, map_id: Optional[int] = None,
                              zone: Optional[int] = None) -> None:
    """Persist the client-reported position of ``guid``.

    Called from the worldserver while the player moves (throttled) and once
    more on logout, so the next login continues where the player logged out.
    ``map_id``/``zone`` are only written when they are not ``None``.
    """
    assignments = ["x = ?", "y = ?", "z = ?", "o = ?"]
    params: List[object] = [float(x), float(y), float(z), float(o)]
    if map_id is not None:
        assignments.append("map = ?")
        params.append(int(map_id))
    if zone is not None:
        assignments.append("zone = ?")
        params.append(int(zone))
    params.append(int(guid))

    con = _connect(path)
    try:
        con.execute(
            f"UPDATE characters SET {', '.join(assignments)} WHERE guid = ?",
            params,
        )
        con.commit()
    finally:
        con.close()


def build_char_enum_payload(characters: List[sqlite3.Row], equipment: Optional[Dict[int, List[Tuple[int, int, int]]]] = None) -> bytes:
    """Serialize ``SMSG_CHAR_ENUM`` for 3.3.5a.
    
    ``equipment`` maps character guid -> list of (display_id, inv_type, enchant_visual)
    for each of the 23 equipment slots. When omitted, all slots are zeros.
    """
    out = bytearray()
    out.append(len(characters) & 0xFF)

    for char in characters:
        guid = int(char["guid"])
        out += struct.pack("<Q", guid)
        out += str(char["name"]).encode("utf-8") + b"\x00"
        out += bytes([
            int(char["race"]) & 0xFF,
            int(char["class"]) & 0xFF,
            int(char["gender"]) & 0xFF,
            int(char["skin"]) & 0xFF,
            int(char["face"]) & 0xFF,
            int(char["hair_style"]) & 0xFF,
            int(char["hair_color"]) & 0xFF,
            int(char["facial_style"]) & 0xFF,
            int(char["level"]) & 0xFF,
        ])
        out += struct.pack("<I", int(char["zone"]))
        out += struct.pack("<I", int(char["map"]))
        out += struct.pack("<fff", float(char["x"]), float(char["y"]), float(char["z"]))
        out += struct.pack("<I", int(char["guild_id"]))
        out += struct.pack("<I", int(char["char_flags"]))
        out += struct.pack("<I", int(char["customize_flags"]))
        out.append(1 if int(char["first_login"]) else 0)
        out += struct.pack("<I", int(char["pet_display_id"]))
        out += struct.pack("<I", int(char["pet_level"]))
        out += struct.pack("<I", int(char["pet_family"]))

        # 23 equipment entries (19 equipment + 4 bags): display id, inventory
        # type, enchant visual.
        char_equipment = equipment.get(guid) if equipment else None
        for i in range(_EQUIPMENT_SLOTS):
            if char_equipment and i < len(char_equipment):
                display_id, inv_type, enchant_visual = char_equipment[i]
            else:
                display_id, inv_type, enchant_visual = 0, 0, 0
            out += struct.pack("<IBI", display_id, inv_type, enchant_visual)

    return bytes(out)


# Starter outfits, derived verbatim from the client's CharStartOutfit.dbc (build
# 12340): key ``(race, class)`` -> ``[(item_entry, inventory_slot), ...]``.
# Slots are the client's INVSLOT_* ids (0..18 equipment, 19..22 bags).  The
# male (sex 0) and female (sex 1) rows carry identical items, so one outfit per
# race/class pair is enough.  Every race/class in ``RACE_CLASSES`` has a row;
# the starting gear of a new character *must* come from here, because 3.3.5a
# gives each combination its own shirt/pants/weapon set.
_STARTER_OUTFITS = {
    (1, 1): [(38, 3), (39, 6), (40, 7), (49778, 15)],
    (1, 2): [(45, 3), (43, 7), (44, 6), (2361, 15)],
    (1, 4): [(49, 3), (47, 7), (48, 6), (28979, 17), (2092, 15), (50055, 16)],
    (1, 5): [(6098, 4), (52, 6), (53, 3), (51, 7), (35, 15)],
    (1, 6): [(34652, 0), (34655, 2), (34659, 14), (34650, 4), (34653, 8), (34649, 9), (34651, 5), (34656, 6), (34648, 7), (34657, 1), (34658, 10), (38145, 19), (38145, 20), (38145, 21), (38145, 22), (38147, 11)],
    (1, 8): [(56, 4), (1395, 6), (55, 7), (35, 15), (6096, 3)],
    (1, 9): [(57, 4), (6097, 3), (1396, 6), (35, 15), (59, 7)],
    (2, 1): [(6125, 3), (139, 6), (140, 7), (12282, 15)],
    (2, 3): [(127, 3), (6126, 6), (6127, 7), (12282, 15), (2101, 19), (2504, 17)],
    (2, 4): [(2105, 3), (120, 6), (121, 7), (25861, 17), (2092, 15), (50055, 16)],
    (2, 6): [(34652, 0), (34655, 2), (34659, 14), (34650, 4), (34653, 8), (34649, 9), (34651, 5), (34656, 6), (34648, 7), (34657, 1), (34658, 10), (38145, 19), (38145, 20), (38145, 21), (38145, 22), (38147, 11)],
    (2, 7): [(154, 3), (153, 6), (36, 15), (2362, 16)],
    (2, 9): [(6129, 4), (1396, 6), (59, 7), (35, 15)],
    (3, 1): [(38, 3), (39, 6), (40, 7), (12282, 15)],
    (3, 2): [(6117, 3), (6118, 6), (43, 7), (2361, 15)],
    (3, 3): [(148, 3), (147, 6), (129, 7), (12282, 15), (2102, 19), (2508, 17)],
    (3, 4): [(2092, 15), (49, 3), (48, 6), (47, 7), (50055, 16), (25861, 17)],
    (3, 5): [(6098, 4), (53, 3), (52, 6), (51, 7), (35, 15)],
    (3, 6): [(34652, 0), (34655, 2), (34659, 14), (34650, 4), (34653, 8), (34649, 9), (34651, 5), (34656, 6), (34648, 7), (34657, 1), (34658, 10), (38145, 19), (38145, 20), (38145, 21), (38145, 22), (38147, 11)],
    (3, 8): [(6116, 4), (1395, 6), (6096, 3), (35, 15), (55, 7)],
    (4, 1): [(49778, 15), (6120, 3), (6121, 6), (6122, 7)],
    (4, 3): [(12282, 15), (148, 3), (147, 6), (129, 7), (2504, 17), (2101, 19)],
    (4, 4): [(2092, 15), (49, 3), (48, 6), (47, 7), (50055, 16), (28979, 17)],
    (4, 5): [(3661, 15), (6119, 4), (52, 6), (51, 7), (53, 3)],
    (4, 6): [(34652, 0), (34655, 2), (34659, 14), (34650, 4), (34653, 8), (34649, 9), (34651, 5), (34656, 6), (34648, 7), (34657, 1), (34658, 10), (38145, 19), (38145, 20), (38145, 21), (38145, 22), (38147, 11)],
    (4, 11): [(3661, 15), (6123, 4), (6124, 6)],
    (5, 1): [(6125, 3), (139, 6), (140, 7), (49778, 15)],
    (5, 4): [(2092, 15), (2105, 3), (120, 6), (121, 7), (50055, 16), (28979, 17)],
    (5, 5): [(6144, 4), (53, 3), (52, 6), (35, 15), (51, 7)],
    (5, 6): [(34652, 0), (34655, 2), (34659, 14), (34650, 4), (34653, 8), (34649, 9), (34651, 5), (34656, 6), (34648, 7), (34657, 1), (34658, 10), (38145, 19), (38145, 20), (38145, 21), (38145, 22), (38147, 11)],
    (5, 8): [(6140, 4), (1395, 6), (6096, 3), (35, 15), (55, 7)],
    (5, 9): [(6129, 4), (1396, 6), (59, 7), (35, 15)],
    (6, 1): [(6125, 3), (139, 6), (2361, 15)],
    (6, 3): [(127, 3), (6126, 6), (12282, 15), (2508, 17), (2102, 19)],
    (6, 6): [(34652, 0), (34655, 2), (34659, 14), (34650, 4), (34653, 8), (34649, 9), (34651, 5), (34656, 6), (34648, 7), (34657, 1), (34658, 10), (38145, 19), (38145, 20), (38145, 21), (38145, 22), (38147, 11)],
    (6, 7): [(154, 3), (153, 6), (36, 15), (2362, 16)],
    (6, 11): [(35, 15), (6139, 4), (6124, 6)],
    (7, 1): [(38, 3), (39, 6), (40, 7), (49778, 15)],
    (7, 4): [(49, 3), (48, 6), (47, 7), (2092, 15), (50055, 16), (28979, 17)],
    (7, 6): [(34652, 0), (34655, 2), (34659, 14), (34650, 4), (34653, 8), (34649, 9), (34651, 5), (34656, 6), (34648, 7), (34657, 1), (34658, 10), (38145, 19), (38145, 20), (38145, 21), (38145, 22), (38147, 11)],
    (7, 8): [(56, 4), (1395, 6), (6096, 3), (55, 7), (35, 15)],
    (7, 9): [(57, 4), (6097, 3), (1396, 6), (59, 7), (35, 15)],
    (8, 1): [(49778, 15), (6125, 3), (139, 6), (25861, 17)],
    (8, 3): [(12282, 15), (2101, 19), (2504, 17), (6126, 6), (127, 3)],
    (8, 4): [(2092, 15), (6136, 3), (6137, 6), (6138, 7), (50055, 16), (25861, 17)],
    (8, 5): [(35, 15), (6144, 4), (53, 3), (52, 6)],
    (8, 6): [(34652, 0), (34655, 2), (34659, 14), (34650, 4), (34653, 8), (34649, 9), (34651, 5), (34656, 6), (34648, 7), (34657, 1), (34658, 10), (38145, 19), (38145, 20), (38145, 21), (38145, 22), (38147, 11)],
    (8, 7): [(36, 15), (6134, 3), (6135, 6), (2362, 16)],
    (8, 8): [(6140, 4), (1395, 6), (6096, 3), (55, 7), (35, 15)],
    (10, 2): [(24143, 3), (24145, 6), (24146, 7), (23346, 15)],
    (10, 3): [(20901, 3), (20899, 6), (20900, 7), (12282, 15), (2101, 19), (20980, 17)],
    (10, 4): [(20982, 15), (20897, 3), (20896, 6), (20898, 7), (50057, 16), (28979, 17)],
    (10, 5): [(20891, 4), (53, 3), (52, 6), (20978, 15), (51, 7)],
    (10, 6): [(34652, 0), (34655, 2), (34659, 14), (34650, 4), (34653, 8), (34649, 9), (34651, 5), (34656, 6), (34648, 7), (34657, 1), (34658, 10), (38145, 19), (38145, 20), (38145, 21), (38145, 22), (38147, 11)],
    (10, 8): [(20893, 4), (20894, 6), (6096, 3), (20978, 15), (20895, 7)],
    (10, 9): [(20892, 4), (1396, 6), (59, 7), (20978, 15), (6097, 3)],
    (11, 1): [(23473, 3), (23474, 6), (23475, 7), (23346, 15)],
    (11, 2): [(23476, 3), (23477, 6), (2361, 15)],
    (11, 3): [(23345, 3), (23344, 6), (12282, 15), (23348, 7), (23347, 17), (2101, 19)],
    (11, 5): [(1396, 6), (6097, 3), (3661, 15), (23322, 4)],
    (11, 6): [(34652, 0), (34655, 2), (34659, 14), (34650, 4), (34653, 8), (34649, 9), (34651, 5), (34656, 6), (34648, 7), (34657, 1), (34658, 10), (38145, 19), (38145, 20), (38145, 21), (38145, 22), (38147, 11)],
    (11, 7): [(23345, 3), (23344, 6), (23348, 7), (36, 15), (2362, 16)],
    (11, 8): [(23478, 6), (23479, 4), (3661, 15), (23473, 3)],
}

# Item templates referenced by ``_STARTER_OUTFITS``.  Columns: entry, class,
# subclass, sound_override_subclass, display_id, quality, inventory_type,
# name1, sell_price, item_level, required_level, material, sheath_type,
# max_durability, damage_min1, damage_max1, delay, container_slots.
# Most rows already come from tools/import_wotlk_db.py; this list guarantees
# the handful of 3.3.5a-only starter items (Worn Greatsword, Worn/Sharp Dirk)
# also exist in a database built from the schema alone.
_STARTER_ITEM_TEMPLATES = [
    (25, 2, 7, 1, 1542, 1, 21, 'Worn Shortsword', 5, 2, 1, 1, 3, 20, 1.0, 3.0, 1900, 0),
    (35, 2, 10, 0, 472, 1, 17, 'Bent Staff', 9, 2, 1, 2, 2, 25, 3.0, 5.0, 2900, 0),
    (36, 2, 4, 0, 5194, 1, 21, 'Worn Mace', 7, 2, 1, 2, 3, 20, 1.0, 3.0, 1900, 0),
    (38, 4, 0, 7, 9891, 1, 4, "Recruit's Shirt", 1, 1, 0, 7, 0, 0, 0.0, 0.0, 0, 0),
    (39, 4, 1, 0, 9892, 0, 7, "Recruit's Pants", 1, 1, 1, 7, 0, 25, 0.0, 0.0, 0, 0),
    (40, 4, 0, 7, 10141, 1, 8, "Recruit's Boots", 1, 1, 0, 7, 0, 0, 0.0, 0.0, 0, 0),
    (43, 4, 0, 0, 9938, 1, 8, "Squire's Boots", 1, 1, 0, 7, 0, 0, 0.0, 0.0, 0, 0),
    (44, 4, 1, 0, 9937, 0, 7, "Squire's Pants", 1, 1, 1, 7, 0, 25, 0.0, 0.0, 0, 0),
    (45, 4, 0, 0, 3265, 1, 4, "Squire's Shirt", 1, 1, 0, 7, 0, 0, 0.0, 0.0, 0, 0),
    (47, 4, 0, 0, 9915, 1, 8, "Footpad's Shoes", 1, 1, 0, 7, 0, 0, 0.0, 0.0, 0, 0),
    (48, 4, 1, 0, 9913, 0, 7, "Footpad's Pants", 1, 1, 1, 7, 0, 25, 0.0, 0.0, 0, 0),
    (49, 4, 0, 0, 9906, 1, 4, "Footpad's Shirt", 1, 1, 0, 7, 0, 0, 0.0, 0.0, 0, 0),
    (51, 4, 0, 0, 9946, 1, 8, "Neophyte's Boots", 1, 1, 0, 7, 0, 0, 0.0, 0.0, 0, 0),
    (52, 4, 1, 0, 9945, 0, 7, "Neophyte's Pants", 1, 1, 1, 7, 0, 25, 0.0, 0.0, 0, 0),
    (53, 4, 0, 0, 9944, 1, 4, "Neophyte's Shirt", 1, 1, 0, 7, 0, 0, 0.0, 0.0, 0, 0),
    (55, 4, 0, 0, 9929, 1, 8, "Apprentice's Boots", 1, 1, 0, 7, 0, 0, 0.0, 0.0, 0, 0),
    (56, 4, 1, 0, 12647, 0, 20, "Apprentice's Robe", 1, 1, 1, 7, 0, 35, 0.0, 0.0, 0, 0),
    (57, 4, 1, 0, 12645, 0, 20, "Acolyte's Robe", 1, 1, 1, 7, 0, 35, 0.0, 0.0, 0, 0),
    (59, 4, 0, 0, 3261, 1, 8, "Acolyte's Shoes", 1, 1, 0, 7, 0, 0, 0.0, 0.0, 0, 0),
    (120, 4, 1, 0, 10006, 0, 7, 'Thug Pants', 1, 1, 1, 7, 0, 25, 0.0, 0.0, 0, 0),
    (121, 4, 0, 0, 10008, 1, 8, 'Thug Boots', 1, 1, 0, 7, 0, 0, 0.0, 0.0, 0, 0),
    (127, 4, 0, 0, 9996, 1, 4, "Trapper's Shirt", 1, 1, 0, 7, 0, 0, 0.0, 0.0, 0, 0),
    (129, 4, 0, 0, 9977, 1, 8, "Rugged Trapper's Boots", 1, 1, 0, 7, 0, 0, 0.0, 0.0, 0, 0),
    (139, 4, 1, 0, 9988, 0, 7, "Brawler's Pants", 1, 1, 1, 7, 0, 25, 0.0, 0.0, 0, 0),
    (140, 4, 0, 0, 9992, 1, 8, "Brawler's Boots", 1, 1, 0, 7, 0, 0, 0.0, 0.0, 0, 0),
    (147, 4, 1, 0, 9975, 0, 7, "Rugged Trapper's Pants", 1, 1, 1, 7, 0, 25, 0.0, 0.0, 0, 0),
    (148, 4, 0, 0, 9976, 1, 4, "Rugged Trapper's Shirt", 1, 1, 0, 7, 0, 0, 0.0, 0.0, 0, 0),
    (153, 4, 2, 0, 10050, 0, 7, 'Primitive Kilt', 1, 1, 1, 8, 0, 30, 0.0, 0.0, 0, 0),
    (154, 4, 0, 0, 10058, 1, 4, 'Primitive Mantle', 1, 1, 0, 7, 0, 0, 0.0, 0.0, 0, 0),
    (1395, 4, 1, 0, 9924, 0, 7, "Apprentice's Pants", 1, 1, 1, 7, 0, 25, 0.0, 0.0, 0, 0),
    (1396, 4, 1, 0, 3260, 0, 7, "Acolyte's Pants", 1, 1, 1, 7, 0, 25, 0.0, 0.0, 0, 0),
    (2092, 2, 15, 0, 6442, 1, 13, 'Worn Dagger', 7, 2, 1, 1, 3, 16, 1.0, 2.0, 1600, 0),
    (2101, 11, 2, 0, 21328, 1, 18, 'Light Quiver', 1, 1, 1, -1, 0, 0, 0.0, 0.0, 0, 6),
    (2102, 11, 3, 0, 1816, 1, 18, 'Small Ammo Pouch', 1, 1, 1, -1, 0, 0, 0.0, 0.0, 0, 6),
    (2105, 4, 0, 0, 10005, 1, 4, 'Thug Shirt', 1, 1, 0, 7, 0, 0, 0.0, 0.0, 0, 0),
    (2361, 2, 5, 0, 8690, 1, 17, 'Battleworn Hammer', 9, 2, 1, 2, 1, 25, 3.0, 5.0, 2900, 0),
    (2362, 4, 6, 0, 18730, 0, 14, 'Worn Wooden Shield', 1, 1, 1, 1, 4, 20, 0.0, 0.0, 0, 0),
    (2504, 2, 2, 0, 8106, 1, 15, 'Worn Shortbow', 5, 2, 1, 2, 0, 20, 5.0, 10.0, 2300, 0),
    (2508, 2, 3, 0, 6606, 1, 26, 'Old Blunderbuss', 5, 2, 1, 1, 0, 20, 5.0, 10.0, 2300, 0),
    (3661, 2, 10, 0, 18530, 1, 17, 'Handcrafted Staff', 9, 2, 1, 2, 2, 25, 3.0, 5.0, 2900, 0),
    (6096, 4, 0, 0, 2163, 1, 4, "Apprentice's Shirt", 1, 1, 0, 7, 0, 0, 0.0, 0.0, 0, 0),
    (6097, 4, 0, 0, 2470, 1, 4, "Acolyte's Shirt", 1, 1, 0, 7, 0, 0, 0.0, 0.0, 0, 0),
    (6098, 4, 1, 0, 12679, 0, 20, "Neophyte's Robe", 1, 1, 1, 7, 0, 35, 0.0, 0.0, 0, 0),
    (6116, 4, 1, 0, 12648, 0, 20, "Apprentice's Robe", 1, 1, 1, 7, 0, 0, 0.0, 0.0, 0, 0),
    (6117, 4, 0, 0, 9972, 1, 4, "Squire's Shirt", 1, 1, 0, 7, 0, 0, 0.0, 0.0, 0, 0),
    (6118, 4, 1, 0, 9974, 0, 7, "Squire's Pants", 1, 1, 1, 7, 0, 25, 0.0, 0.0, 0, 0),
    (6119, 4, 1, 0, 12681, 0, 20, "Neophyte's Robe", 1, 1, 1, 7, 0, 35, 0.0, 0.0, 0, 0),
    (6120, 4, 0, 0, 9983, 1, 4, "Recruit's Shirt", 1, 1, 0, 7, 0, 0, 0.0, 0.0, 0, 0),
    (6121, 4, 1, 0, 9984, 0, 7, "Recruit's Pants", 1, 1, 1, 7, 0, 25, 0.0, 0.0, 0, 0),
    (6122, 4, 0, 0, 9985, 1, 8, "Recruit's Boots", 1, 1, 0, 7, 0, 0, 0.0, 0.0, 0, 0),
    (6123, 4, 1, 0, 12683, 0, 20, "Novice's Robe", 1, 1, 1, 7, 0, 35, 0.0, 0.0, 0, 0),
    (6124, 4, 1, 0, 9987, 0, 7, "Novice's Pants", 1, 1, 1, 7, 0, 25, 0.0, 0.0, 0, 0),
    (6125, 4, 0, 0, 9995, 1, 4, "Brawler's Harness", 1, 1, 0, 7, 0, 0, 0.0, 0.0, 0, 0),
    (6126, 4, 1, 0, 10002, 0, 7, "Trapper's Pants", 1, 1, 1, 7, 0, 25, 0.0, 0.0, 0, 0),
    (6127, 4, 0, 0, 10003, 1, 8, "Trapper's Boots", 1, 1, 0, 7, 0, 0, 0.0, 0.0, 0, 0),
    (6129, 4, 1, 0, 12646, 0, 20, "Acolyte's Robe", 1, 1, 1, 7, 0, 35, 0.0, 0.0, 0, 0),
    (6134, 4, 0, 0, 10108, 1, 4, 'Primitive Mantle', 1, 1, 0, 7, 0, 0, 0.0, 0.0, 0, 0),
    (6135, 4, 2, 0, 10109, 0, 7, 'Primitive Kilt', 1, 1, 1, 8, 0, 30, 0.0, 0.0, 0, 0),
    (6136, 4, 0, 0, 10112, 1, 4, 'Thug Shirt', 1, 1, 0, 7, 0, 0, 0.0, 0.0, 0, 0),
    (6137, 4, 1, 0, 10114, 0, 7, 'Thug Pants', 1, 1, 1, 7, 0, 25, 0.0, 0.0, 0, 0),
    (6138, 4, 0, 0, 10115, 1, 8, 'Thug Boots', 1, 1, 0, 7, 0, 0, 0.0, 0.0, 0, 0),
    (6139, 4, 1, 0, 12684, 0, 20, "Novice's Robe", 1, 1, 1, 7, 0, 35, 0.0, 0.0, 0, 0),
    (6140, 4, 1, 0, 12649, 0, 20, "Apprentice's Robe", 1, 1, 1, 7, 0, 35, 0.0, 0.0, 0, 0),
    (6144, 4, 1, 0, 12680, 0, 20, "Neophyte's Robe", 1, 1, 1, 7, 0, 35, 0.0, 0.0, 0, 0),
    (12282, 2, 1, 0, 22291, 1, 17, 'Worn Battleaxe', 8, 2, 1, 1, 1, 25, 3.0, 5.0, 2900, 0),
    (20891, 4, 1, 0, 33256, 0, 20, "Neophyte's Robe", 1, 1, 1, 7, 0, 35, 0.0, 0.0, 0, 0),
    (20892, 4, 1, 0, 33257, 0, 20, "Acolyte's Robe", 1, 1, 1, 7, 0, 35, 0.0, 0.0, 0, 0),
    (20893, 4, 1, 0, 33258, 0, 20, "Apprentice's Robe", 1, 1, 1, 7, 0, 35, 0.0, 0.0, 0, 0),
    (20894, 4, 1, 0, 33259, 0, 7, "Apprentice's Pants", 1, 1, 1, 7, 0, 25, 0.0, 0.0, 0, 0),
    (20895, 4, 0, 0, 33260, 1, 8, "Apprentice's Boots", 1, 1, 0, 7, 0, 0, 0.0, 0.0, 0, 0),
    (20896, 4, 1, 0, 33342, 0, 7, "Lookout's Pants", 1, 1, 0, 7, 0, 25, 0.0, 0.0, 0, 0),
    (20897, 4, 0, 0, 33353, 0, 4, "Lookout's Tunic", 1, 1, 0, 7, 0, 0, 0.0, 0.0, 0, 0),
    (20898, 4, 1, 0, 36764, 0, 8, "Lookout's Shoes", 1, 1, 0, 7, 0, 16, 0.0, 0.0, 0, 0),
    (20899, 4, 1, 0, 33351, 0, 7, "Warder's Pants", 1, 1, 1, 7, 0, 25, 0.0, 0.0, 0, 0),
    (20900, 4, 1, 0, 33350, 0, 8, "Warder's Boots", 1, 1, 0, 7, 0, 16, 0.0, 0.0, 0, 0),
    (20901, 4, 0, 0, 33352, 0, 4, "Warder's Shirt", 1, 1, 0, 7, 0, 0, 0.0, 0.0, 0, 0),
    (20978, 2, 10, 0, 33299, 1, 17, "Apprentice's Staff", 9, 2, 1, 2, 2, 25, 3.0, 5.0, 2900, 0),
    (20980, 2, 2, 0, 8106, 1, 15, "Warder's Shortbow", 5, 2, 1, 2, 0, 20, 5.0, 10.0, 2300, 0),
    (20982, 2, 15, 0, 37170, 1, 13, 'Sharp Dagger', 7, 2, 1, 1, 3, 16, 1.0, 2.0, 1600, 0),
    (23322, 4, 1, 0, 36138, 0, 20, "Acolyte's Robe", 1, 1, 1, 7, 0, 35, 0.0, 0.0, 0, 0),
    (23344, 4, 1, 0, 36120, 0, 7, "Scout's Pants", 1, 1, 1, 7, 0, 25, 0.0, 0.0, 0, 0),
    (23345, 4, 0, 0, 36118, 1, 4, "Scout's Shirt", 1, 1, 0, 7, 0, 0, 0.0, 0.0, 0, 0),
    (23346, 2, 8, 0, 2380, 1, 17, 'Battleworn Claymore', 9, 2, 1, 1, 1, 25, 3.0, 5.0, 2900, 0),
    (23347, 2, 18, 0, 10671, 1, 26, 'Weathered Crossbow', 5, 2, 1, 2, 0, 20, 6.0, 10.0, 2500, 0),
    (23348, 4, 0, 0, 36122, 1, 8, "Scout's Boots", 1, 1, 0, 7, 0, 0, 0.0, 0.0, 0, 0),
    (23473, 4, 0, 0, 36124, 1, 4, "Recruit's Shirt", 1, 1, 0, 7, 0, 0, 0.0, 0.0, 0, 0),
    (23474, 4, 1, 0, 36125, 0, 7, "Recruit's Pants", 1, 1, 1, 7, 0, 25, 0.0, 0.0, 0, 0),
    (23475, 4, 0, 0, 36126, 1, 8, "Recruit's Boots", 1, 1, 0, 7, 0, 0, 0.0, 0.0, 0, 0),
    (23476, 4, 0, 0, 36133, 1, 4, "Squire's Shirt", 1, 1, 0, 7, 0, 0, 0.0, 0.0, 0, 0),
    (23477, 4, 1, 0, 36134, 0, 7, "Squire's Pants", 1, 1, 1, 7, 0, 25, 0.0, 0.0, 0, 0),
    (23478, 4, 1, 0, 36125, 0, 7, "Recruit's Pants", 1, 1, 1, 7, 0, 25, 0.0, 0.0, 0, 0),
    (23479, 4, 1, 0, 36140, 0, 20, "Recruit's Robe", 1, 1, 1, 7, 0, 35, 0.0, 0.0, 0, 0),
    (24143, 4, 0, 0, 36789, 0, 4, "Initiate's Shirt", 1, 1, 0, 7, 0, 0, 0.0, 0.0, 0, 0),
    (24145, 4, 1, 0, 36790, 0, 7, "Initiate's Pants", 1, 1, 1, 7, 0, 25, 0.0, 0.0, 0, 0),
    (24146, 4, 1, 0, 36793, 0, 8, "Initiate's Boots", 1, 1, 0, 7, 0, 16, 0.0, 0.0, 0, 0),
    (25861, 2, 16, 0, 20777, 1, 25, 'Crude Throwing Axe', 3, 3, 1, 1, 0, 0, 2.0, 4.0, 1800, 0),
    (28979, 2, 16, 0, 40187, 1, 25, 'Light Throwing Knife', 3, 3, 1, 1, 0, 0, 1.0, 3.0, 1600, 0),
    (34648, 4, 4, 0, 51496, 2, 8, "Acherus Knight's Greaves", 10, 60, 55, 6, 0, 55, 0.0, 0.0, 0, 0),
    (34649, 4, 4, 0, 51498, 2, 10, "Acherus Knight's Gauntlets", 6, 60, 55, 6, 0, 40, 0.0, 0.0, 0, 0),
    (34650, 4, 4, 0, 51494, 2, 5, "Acherus Knight's Tunic", 13, 60, 55, 6, 0, 115, 0.0, 0.0, 0, 0),
    (34651, 4, 4, 0, 51497, 2, 6, "Acherus Knight's Girdle", 7, 60, 55, 6, 0, 40, 0.0, 0.0, 0, 0),
    (34652, 4, 4, 0, 51495, 2, 1, "Acherus Knight's Hood", 10, 60, 55, 6, 0, 70, 0.0, 0.0, 0, 0),
    (34653, 4, 4, 0, 51500, 2, 9, "Acherus Knight's Wristguard", 7, 60, 55, 6, 0, 40, 0.0, 0.0, 0, 0),
    (34655, 4, 4, 0, 51501, 2, 3, "Acherus Knight's Pauldrons", 10, 60, 55, 6, 0, 70, 0.0, 0.0, 0, 0),
    (34656, 4, 4, 0, 51499, 2, 7, "Acherus Knight's Legplates", 14, 60, 55, 6, 0, 85, 0.0, 0.0, 0, 0),
    (34657, 4, 0, 0, 6539, 2, 2, 'Choker of Damnation', 575, 60, 55, 3, 0, 0, 0.0, 0.0, 0, 0),
    (34658, 4, 0, 0, 963, 2, 11, 'Plague Band', 133, 60, 55, 7, 0, 0, 0.0, 0.0, 0, 0),
    (34659, 4, 1, 0, 49738, 2, 16, "Acherus Knight's Shroud", 6, 60, 55, 7, 0, 0, 0.0, 0.0, 0, 0),
    (38145, 1, 0, 0, 1282, 1, 18, 'Deathweave Bag', 0, 35, 0, 8, 0, 0, 0.0, 0.0, 0, 12),
    (38147, 4, 0, 0, 963, 2, 11, 'Corrupted Band', 133, 60, 55, 7, 0, 0, 0.0, 0.0, 0, 0),
    (49778, 2, 8, 1, 2380, 1, 17, 'Worn Greatsword', 9, 2, 1, 1, 1, 25, 3.0, 5.0, 2900, 0),
    (50055, 2, 15, 1, 6442, 1, 22, 'Worn Dirk', 7, 2, 1, 1, 3, 16, 1.0, 2.0, 1600, 0),
    (50057, 2, 15, 1, 37170, 1, 22, 'Sharp Dirk', 7, 2, 1, 1, 3, 16, 1.0, 2.0, 1600, 0),
]

# Item.dbc "Sheath" values; they match the client's SHEATHE_TYPE enum
# (WoW/Common/ComponentCore/Types.h).  They are not cosmetic: with the default
# sheathed weapon mode (WEAPONMODE_SHEATHEDMODE == 0) the client attaches a
# weapon through CCharacterComponent::GetSheatheLink(sheatheType, mainHand),
# and SHEATHE_NONE (0) resolves to ATTACH_NONE.  AddHandItem then sees no
# attachment and returns without creating the model, so an equipped weapon with
# sheath type 0 is invisible.  Armour never uses that path and keeps 0.
SHEATHE_NONE = 0
SHEATHE_MAINHAND = 1
SHEATHE_BACK = 2
SHEATHE_HIP = 3
SHEATHE_SHIELD = 4


def default_sheath_type(inventory_type: int) -> int:
    """Return the Item.dbc sheath type for an inventory type.

    Used when the item template does not carry one (and to backfill databases
    seeded before the column existed).  Only weapons/off-hands need it.
    """
    if inventory_type in (13, 21, 22, 23):
        # One-hand weapon, main-hand, off-hand, held in off-hand.
        return SHEATHE_HIP
    if inventory_type == 17:        # two-hand weapon
        return SHEATHE_BACK
    if inventory_type == 14:        # shield
        return SHEATHE_SHIELD
    if inventory_type in (15, 26):  # bow, gun/crossbow/wand
        return SHEATHE_BACK
    if inventory_type == 25:        # thrown
        return SHEATHE_HIP
    return SHEATHE_NONE


def _seed_starter_items(con: sqlite3.Connection) -> None:
    """Insert/backfill every item template the starter outfits reference.

    ``item_templates`` normally comes from ``tools/import_wotlk_db.py``; this
    only fills rows the dump lacks (notably the 3.3.5a-only Worn Greatsword and
    Worn/Sharp Dirk) and backfills columns the dump leaves at zero.
    """
    existing = {row[0] for row in con.execute("SELECT entry FROM item_templates")}
    for item in _STARTER_ITEM_TEMPLATES:
        (entry, item_class, subclass, sound_override, display_id, quality,
         inv_type, name, sell_price, item_level, required_level, material,
         sheath_type, max_durability, damage_min, damage_max, delay,
         container_slots) = item
        if entry in existing:
            # A zero sheath type makes the client skip the weapon attachment
            # entirely; the item import also does not carry a sound column.
            con.execute(
                "UPDATE item_templates SET"
                " sheath_type = CASE WHEN sheath_type = 0 THEN ? ELSE sheath_type END,"
                " sound_override_subclass = CASE WHEN sound_override_subclass = 0"
                "     THEN ? ELSE sound_override_subclass END,"
                " container_slots = CASE WHEN container_slots = 0 THEN ?"
                "     ELSE container_slots END"
                " WHERE entry = ?",
                (sheath_type, sound_override, container_slots, entry),
            )
            continue
        con.execute(
            """INSERT INTO item_templates(
                entry, class, subclass, sound_override_subclass, display_id, quality,
                inventory_type, name1, sell_price, item_level, required_level,
                material, sheath_type, max_durability, damage_min1, damage_max1,
                delay, container_slots
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            item,
        )


# Bump whenever the *format* of a cached query response changes without the
# underlying rows changing (for example creature names now carry the NPC job
# title).  The client compares this version with the header of its WDB files
# and discards them on a mismatch, which forces a fresh creature query.
CACHE_FORMAT_VERSION = 4


def client_cache_version(path: str) -> int:
    """Return the value for ``SMSG_CLIENTCACHE_VERSION``.

    The 3.3.5 client stores item stats in ``Cache/WDB/<locale>/itemcache.wdb``
    and only asks the server again when the version in that file differs from
    the one sent at login (``DBCache<T>::VerifyAndUpdateVersion``).  Sending a
    constant means edited item data (for example a weapon that gained its
    sheath type) is never reloaded: the stale cached copy wins.

    A full import stores ~27k templates with 129 columns, so the version is
    derived from SQL aggregates over the fields the client actually caches
    instead of hashing every row in Python.
    """
    con = _connect(path)
    try:
        row = con.execute(
            """SELECT COUNT(*),
                      COALESCE(SUM(entry * 31 + display_id), 0),
                      COALESCE(SUM(quality), 0),
                      COALESCE(SUM(inventory_type), 0),
                      COALESCE(SUM(CAST(damage_min1 * 100 AS INTEGER)), 0),
                      COALESCE(SUM(CAST(damage_max1 * 100 AS INTEGER)), 0),
                      COALESCE(SUM(delay), 0),
                      COALESCE(SUM(sheath_type), 0),
                      COALESCE(SUM(max_durability), 0),
                      COALESCE(SUM(required_level), 0),
                      COALESCE(SUM(item_level), 0),
                      COALESCE(SUM(class * 17 + subclass), 0),
                      COALESCE(SUM(buy_price), 0),
                      COALESCE(SUM(LENGTH(name1)), 0),
                      COALESCE(SUM(LENGTH(description)), 0),
                      COALESCE(MAX(entry), 0)
               FROM item_templates"""
        ).fetchone()
    finally:
        con.close()
    return (zlib.crc32(
        repr((CACHE_FORMAT_VERSION,) + tuple(row)).encode("utf-8")
    ) & 0xFFFFFFFF) or 1


def get_item_template(path: str, entry: int) -> Optional[sqlite3.Row]:
    """Fetch an item template by entry."""
    con = _connect(path)
    con.row_factory = sqlite3.Row
    try:
        return con.execute(
            "SELECT * FROM item_templates WHERE entry = ?", (entry,)
        ).fetchone()
    finally:
        con.close()


def get_character_inventory(path: str, character_guid: int) -> Dict[int, sqlite3.Row]:
    """Return {slot: item_row} for all items owned by a character.

    Each row also carries ``display_id`` from ``item_templates`` (NULL when the
    template is missing) so callers can build visual packets such as
    ``SMSG_MIRRORIMAGE_DATA`` without a second query per item.
    """
    con = _connect(path)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            """SELECT ci.*, it.display_id AS display_id
               FROM character_items ci
               LEFT JOIN item_templates it ON ci.item_entry = it.entry
               WHERE ci.character_guid = ?""",
            (character_guid,),
        ).fetchall()
        return {int(row["slot"]): row for row in rows}
    finally:
        con.close()


# 3.3.5a ``ItemModType`` values that map directly onto a primary attribute.
_ITEM_PRIMARY_STATS = {
    3: "agi",      # ITEM_MOD_AGILITY
    4: "str",      # ITEM_MOD_STRENGTH
    5: "inte",     # ITEM_MOD_INTELLECT
    6: "spi",      # ITEM_MOD_SPIRIT
    7: "sta",      # ITEM_MOD_STAMINA
}

# The seven resistance slots in ``UNIT_FIELD_RESISTANCES``: physical armor first,
# then the six magic schools in SpellSchools order.
_ITEM_RESISTANCES = (
    ("armor", "resistance_armor"),
    ("holy", "resistance_holy"),
    ("fire", "resistance_fire"),
    ("nature", "resistance_nature"),
    ("frost", "resistance_frost"),
    ("shadow", "resistance_shadow"),
    ("arcane", "resistance_arcane"),
)


def get_equipped_item_totals(path: str, character_guid: int) -> Dict[str, int]:
    """Aggregate the bonuses of every equipped item (slots 0..18).

    Only the 19 equipment slots count.  Bags (19..22) and backpack slots
    (256..271) are deliberately excluded: their contents are carried, not worn.
    The result mirrors the auras CMaNGOS applies from items:

    ``str``/``agi``/``sta``/``inte``/``spi``
        Primary-attribute bonuses (``ITEM_MOD_*`` 3..7).
    ``health``/``mana``
        Flat ``ITEM_MOD_HEALTH``/``ITEM_MOD_MANA`` pools.
    ``attack_power``/``ranged_attack_power``
        Flat ``ITEM_MOD_ATTACK_POWER``/``ITEM_MOD_RANGED_ATTACK_POWER``.
    ``armor`` plus ``holy``/``fire``/``nature``/``frost``/``shadow``/``arcane``
        Item resistances.  The physical slot is what the client shows as Armor;
        the agility contribution is added later by ``UpdateArmor`` logic in the
        session, not here.

    Missing templates or a schema-only database yield an all-zero dict, so
    callers never have to special-case a character with no gear.
    """
    totals: Dict[str, int] = {
        "str": 0, "agi": 0, "sta": 0, "inte": 0, "spi": 0,
        "health": 0, "mana": 0,
        "attack_power": 0, "ranged_attack_power": 0, "spell_power": 0,
        "hit_rating": 0, "crit_rating": 0, "haste_rating": 0,
        "defense_rating": 0, "dodge_rating": 0, "parry_rating": 0,
        "block_rating": 0, "block_value": 0,
        "resilience": 0, "armor_penetration": 0, "expertise": 0,
        "spell_penetration": 0, "healing_power": 0,
        "shield": 0,
        "armor": 0, "holy": 0, "fire": 0, "nature": 0,
        "frost": 0, "shadow": 0, "arcane": 0,
    }
    stat_columns = ", ".join(
        f"it.stat_type{i}, it.stat_value{i}" for i in range(1, 11)
    )
    resistance_columns = ", ".join(
        f"it.{column}" for _key, column in _ITEM_RESISTANCES
    )
    con = _connect(path)
    try:
        rows = con.execute(
            f"SELECT {resistance_columns}, {stat_columns},"
            " it.subclass, it.inventory_type "
            "FROM character_items ci "
            "JOIN item_templates it ON ci.item_entry = it.entry "
            "WHERE ci.character_guid = ? AND ci.slot BETWEEN 0 AND 18",
            (int(character_guid),),
        ).fetchall()
    except sqlite3.Error:
        return totals
    finally:
        con.close()

    for row in rows:
        for index, (key, _column) in enumerate(_ITEM_RESISTANCES):
            totals[key] += int(row[index] or 0)
        offset = len(_ITEM_RESISTANCES)
        subclass = int(row[offset + 20] or 0)
        inventory_type = int(row[offset + 21] or 0)
        if subclass == 6 or inventory_type == 14:      # shield
            totals["shield"] = 1
        for stat_index in range(10):
            stat_type = int(row[offset + stat_index * 2] or 0)
            stat_value = int(row[offset + stat_index * 2 + 1] or 0)
            if not stat_value:
                continue
            if stat_type in _ITEM_PRIMARY_STATS:
                totals[_ITEM_PRIMARY_STATS[stat_type]] += stat_value
            elif stat_type == 0:            # ITEM_MOD_MANA
                totals["mana"] += stat_value
            elif stat_type == 1:            # ITEM_MOD_HEALTH
                totals["health"] += stat_value
            elif stat_type == 38:           # ITEM_MOD_ATTACK_POWER
                totals["attack_power"] += stat_value
            elif stat_type == 39:           # ITEM_MOD_RANGED_ATTACK_POWER
                totals["ranged_attack_power"] += stat_value
            elif stat_type == 45:           # ITEM_MOD_SPELL_POWER
                totals["spell_power"] += stat_value
            elif stat_type == 12:           # ITEM_MOD_DEFENSE_SKILL_RATING
                totals["defense_rating"] += stat_value
            elif stat_type == 13:           # ITEM_MOD_DODGE_RATING
                totals["dodge_rating"] += stat_value
            elif stat_type == 14:           # ITEM_MOD_PARRY_RATING
                totals["parry_rating"] += stat_value
            elif stat_type == 15:           # ITEM_MOD_BLOCK_RATING
                totals["block_rating"] += stat_value
            elif stat_type in (16, 17, 18, 31):  # melee/ranged/spell hit rating
                totals["hit_rating"] += stat_value
            elif stat_type in (19, 20, 21, 32):  # melee/ranged/spell crit rating
                totals["crit_rating"] += stat_value
            elif stat_type in (28, 29, 30, 36):  # melee/ranged/spell haste rating
                totals["haste_rating"] += stat_value
            elif stat_type == 35:           # ITEM_MOD_RESILIENCE_RATING
                totals["resilience"] += stat_value
            elif stat_type == 37:           # ITEM_MOD_EXPERTISE_RATING
                totals["expertise"] += stat_value
            elif stat_type == 44:           # ITEM_MOD_ARMOR_PENETRATION_RATING
                totals["armor_penetration"] += stat_value
            elif stat_type == 47:           # ITEM_MOD_SPELL_PENETRATION
                totals["spell_penetration"] += stat_value
            elif stat_type in (41, 42):     # healing / spell damage
                totals["healing_power"] += stat_value
            elif stat_type == 48:           # ITEM_MOD_BLOCK_VALUE
                totals["block_value"] += stat_value
    return totals


def get_equipment_for_char_enum(path: str, character_guid: int) -> List[Tuple[int, int, int]]:
    """Build the 23-slot equipment block for SMSG_CHAR_ENUM.
    
    Returns [(display_id, inv_type, enchant_visual), ...] for slots 0..22.
    """
    con = _connect(path)
    con.row_factory = sqlite3.Row
    try:
        # Join items with templates to get display_id and inventory_type
        rows = con.execute(
            """SELECT ci.slot, it.display_id, it.inventory_type, 0 as enchant_visual
               FROM character_items ci
               JOIN item_templates it ON ci.item_entry = it.entry
               WHERE ci.character_guid = ? AND ci.slot BETWEEN 0 AND 22
               ORDER BY ci.slot""",
            (character_guid,),
        ).fetchall()
        # Build full 23-slot array
        result: List[Tuple[int, int, int]] = [(0, 0, 0)] * _EQUIPMENT_SLOTS
        for row in rows:
            slot = int(row["slot"])
            if 0 <= slot < _EQUIPMENT_SLOTS:
                result[slot] = (int(row["display_id"]), int(row["inventory_type"]), int(row["enchant_visual"]))
        return result
    finally:
        con.close()


def create_character_items(path: str, character_guid: int, items: List[Tuple[int, int]]) -> List[Dict]:
    """Create item instances for a character.
    
    ``items`` is a list of (item_entry, slot) tuples.
    Returns list of created items with their DB IDs and assigned slots.
    """
    con = _connect(path)
    try:
        created_items = []
        for entry, slot in items:
            template = con.execute(
                "SELECT max_durability FROM item_templates WHERE entry = ?", (entry,)
            ).fetchone()
            max_dur = int(template[0]) if template else 0
            cur = con.execute(
                """INSERT INTO character_items(
                    character_guid, item_entry, slot, stack_count, durability, max_durability
                ) VALUES(?,?,?,?,?,?)""",
                (character_guid, entry, slot, 1, max_dur, max_dur),
            )
            con.commit()
            item_id = cur.lastrowid
            created_items.append({
                "id": item_id,
                "item_entry": entry,
                "slot": slot,
                "stack_count": 1,
                "durability": max_dur,
                "max_durability": max_dur,
            })
        return created_items
    finally:
        con.close()


def add_item_to_inventory(path: str, character_guid: int, entry: int,
                          count: int = 1) -> Optional[List[Dict]]:
    """Store ``count`` of ``entry`` in the character's backpack.

    Existing partial stacks are filled first (when the item is stackable) and
    any remainder goes into the first free backpack slot(s).  Backpack slots
    are encoded as 256..271.

    Returns a list of change records, one per affected stack::

        {"id", "item_entry", "slot", "added", "stack_count",
         "durability", "max_durability", "created"}

    ``created`` is true for a newly inserted row (the caller must send an item
    create block) and false for an existing stack (only the stack count field
    changed).  The whole move is atomic: when all of ``count`` cannot be
    stored, nothing is written and ``None`` is returned.
    """
    con = _connect(path)
    try:
        template = con.execute(
            "SELECT stackable, max_durability FROM item_templates WHERE entry = ?",
            (entry,),
        ).fetchone()
        if template is None:
            return None                     # unknown item: never hand it out
        stackable = max(1, int(template[0] or 1))
        max_dur = int(template[1] or 0)
        remaining = max(1, int(count))

        rows = con.execute(
            "SELECT id, slot, stack_count, item_entry FROM character_items "
            "WHERE character_guid = ?",
            (character_guid,),
        ).fetchall()
        used = {int(row[1]) for row in rows}

        changes: List[Dict] = []
        if stackable > 1:
            for item_id, slot, stack_count, row_entry in rows:
                # Only a partial stack of the *same* item can take more.
                if int(row_entry) != int(entry):
                    continue
                slot = int(slot)
                if not (256 <= slot <= 271):
                    continue
                current = int(stack_count or 1)
                if current >= stackable:
                    continue
                add = min(remaining, stackable - current)
                if add <= 0:
                    continue
                changes.append({
                    "id": int(item_id),
                    "item_entry": int(entry),
                    "slot": slot,
                    "added": add,
                    "stack_count": current + add,
                    "durability": max_dur,
                    "max_durability": max_dur,
                    "created": False,
                })
                remaining -= add
                if remaining <= 0:
                    break

        if remaining > 0:
            free = [slot for slot in range(256, 272) if slot not in used]
            needed = (remaining + stackable - 1) // stackable
            if needed > len(free):
                return None                 # backpack full: leave loot on corpse
            for slot in free[:needed]:
                amount = min(remaining, stackable)
                changes.append({
                    "id": 0,               # filled in after INSERT
                    "item_entry": int(entry),
                    "slot": slot,
                    "added": amount,
                    "stack_count": amount,
                    "durability": max_dur,
                    "max_durability": max_dur,
                    "created": True,
                })
                remaining -= amount
                if remaining <= 0:
                    break

        # All-or-nothing: apply only after the capacity check succeeded.
        for change in changes:
            if change["created"]:
                cur = con.execute(
                    """INSERT INTO character_items(
                        character_guid, item_entry, slot, stack_count, durability,
                        max_durability
                    ) VALUES(?,?,?,?,?,?)""",
                    (character_guid, entry, change["slot"], change["stack_count"],
                     max_dur, max_dur),
                )
                change["id"] = int(cur.lastrowid)
            else:
                con.execute(
                    "UPDATE character_items SET stack_count = ? "
                    "WHERE character_guid = ? AND slot = ?",
                    (change["stack_count"], character_guid, change["slot"]),
                )
        con.commit()
        return changes
    finally:
        con.close()


# Fallback spellbook used only when ``playercreateinfo_spell`` is empty (for
# example a database created before playercreateinfo.sql was loaded).  The
# authoritative, race-specific list lives in ``playercreateinfo.sql``; this
# table keeps each class functional with its real level-1 abilities, armor and
# weapon proficiencies.
_STARTER_SPELLS_BY_CLASS = {
    1: [  # Warrior
        78,     # Heroic Strike
        81,     # Dodge
        107,    # Block
        196,    # One-Handed Axes
        197,    # Two-Handed Axes
        198,    # One-Handed Maces
        199,    # Two-Handed Maces
        201,    # One-Handed Swords
        202,    # Two-Handed Swords
        203,    # Unarmed
        204,    # Defense
        1180,   # Daggers
        2457,   # Battle Stance
        2567,   # Thrown
        2764,   # Throw
        6603,   # Attack
        8737,   # Mail
        9077,   # Leather
        9116,   # Shield
    ],
    2: [  # Paladin
        81,     # Dodge
        107,    # Block
        198,    # One-Handed Maces
        199,    # Two-Handed Maces
        201,    # One-Handed Swords
        202,    # Two-Handed Swords
        203,    # Unarmed
        204,    # Defense
        635,    # Holy Light
        6603,   # Attack
        8737,   # Mail
        9077,   # Leather
        9116,   # Shield
        21084,  # Seal of Righteousness
        27762,  # Libram
    ],
    3: [  # Hunter
        75,     # Auto Shot
        81,     # Dodge
        196,    # One-Handed Axes
        197,    # Two-Handed Axes
        201,    # One-Handed Swords
        203,    # Unarmed
        204,    # Defense
        264,    # Bows
        266,    # Guns
        1180,   # Daggers
        2973,   # Raptor Strike
        5011,   # Crossbows
        6603,   # Attack
        9077,   # Leather
    ],
    4: [  # Rogue
        81,     # Dodge
        203,    # Unarmed
        204,    # Defense
        674,    # Dual Wield
        1180,   # Daggers
        1752,   # Sinister Strike
        2098,   # Eviscerate
        2567,   # Thrown
        2764,   # Throw
        6603,   # Attack
        9077,   # Leather
        16092,  # Defensive State (DND)
        21184,  # Rogue Passive (DND)
    ],
    5: [  # Priest
        81,     # Dodge
        198,    # One-Handed Maces
        203,    # Unarmed
        204,    # Defense
        227,    # Staves
        585,    # Smite
        2050,   # Lesser Heal
        5009,   # Wands
        5019,   # Shoot
        6603,   # Attack
    ],
    6: [  # Death Knight (starts at level 55, so the kit is much larger)
        81,     # Dodge
        196,    # One-Handed Axes
        197,    # Two-Handed Axes
        200,    # Polearms
        201,    # One-Handed Swords
        202,    # Two-Handed Swords
        203,    # Unarmed
        204,    # Defense
        674,    # Dual Wield
        750,    # Plate Mail
        3127,   # Parry
        6603,   # Attack
        8737,   # Mail
        9077,   # Leather
        45462,  # Plague Strike
        45477,  # Icy Touch
        45902,  # Blood Strike
        45903,  # Offensive State (DND)
        47541,  # Death Coil
        48266,  # Blood Presence
        49410,  # Forceful Deflection
        49576,  # Death Grip
        52665,  # Sigil
        56816,  # Rune Strike
        59879,  # Blood Plague
        59921,  # Frost Fever
        61455,  # Runic Focus
    ],
    7: [  # Shaman
        81,     # Dodge
        107,    # Block
        198,    # One-Handed Maces
        203,    # Unarmed
        204,    # Defense
        227,    # Staves
        331,    # Healing Wave
        403,    # Lightning Bolt
        6603,   # Attack
        9077,   # Leather
        9116,   # Shield
        27763,  # Totem
        75461,  # Flame Shock Passive
    ],
    8: [  # Mage
        81,     # Dodge
        133,    # Fireball
        168,    # Frost Armor
        203,    # Unarmed
        204,    # Defense
        227,    # Staves
        5009,   # Wands
        5019,   # Shoot
        6603,   # Attack
        71761,  # Deep Freeze Immunity State
    ],
    9: [  # Warlock
        81,     # Dodge
        203,    # Unarmed
        204,    # Defense
        227,    # Staves
        686,    # Shadow Bolt
        687,    # Demon Skin
        1180,   # Daggers
        5009,   # Wands
        5019,   # Shoot
        6603,   # Attack
        18822,  # Improved Enslave Demon
        58284,  # Chaos Bolt Passive
        75445,  # Demonic Immolate
    ],
    11: [  # Druid
        81,     # Dodge
        198,    # One-Handed Maces
        203,    # Unarmed
        204,    # Defense
        227,    # Staves
        1180,   # Daggers
        5176,   # Wrath
        5185,   # Healing Touch
        6603,   # Attack
        9077,   # Leather
    ],
}

# Abilities a level-1 class trainer would teach but ``playercreateinfo_spell``
# does not grant.  This server has no trainers, so summoning would otherwise be
# unreachable for a warlock; the exception is deliberately kept tiny.
_EXTRA_STARTER_SPELLS_BY_CLASS = {
    9: [688],   # Summon Imp (level-1 trainer spell)
}


def _grant_starter_spells(con: sqlite3.Connection, character_guid: int,
                          race: int, char_class: int) -> None:
    """Grant the authentic starter spellbook to a new character."""
    for spell_id in _starter_spells_for(con, int(race), int(char_class)):
        con.execute(
            "INSERT OR IGNORE INTO character_spells(character_guid, spell, active) VALUES(?,?,1)",
            (character_guid, int(spell_id)),
        )


def grant_starter_items(path: str, character_guid: int, race: int,
                        char_class: int) -> List[Dict]:
    """Grant a new character the exact 3.3.5a outfit for its race/class.

    Gear comes from ``_STARTER_OUTFITS`` (the client's own CharStartOutfit.dbc)
    so a human warrior gets the two-hander recruit set, an orc warrior the
    battleaxe set, a mage the robe set, and so on.  Slots are the client's
    INVSLOT_* ids; the four bag slots (19-22) are normal container items and
    the login create block already sends their item objects.

    Starter spells for the character's class are granted in the same
    transaction.  Returns the list of created item rows.
    """
    items = list(_STARTER_OUTFITS.get((int(race), int(char_class)), ()))
    created = create_character_items(path, character_guid, items)

    # Also grant starter spells
    con = _connect(path)
    try:
        _grant_starter_spells(con, character_guid, race, char_class)
        con.commit()
    finally:
        con.close()

    return created


def delete_character_item(path: str, character_guid: int, slot: int) -> bool:
    """Delete a single item from a character's inventory."""
    con = _connect(path)
    try:
        cur = con.execute(
            "DELETE FROM character_items WHERE character_guid = ? AND slot = ?",
            (character_guid, slot),
        )
        con.commit()
        return cur.rowcount > 0
    finally:
        con.close()


def swap_character_items(path: str, character_guid: int, slot_a: int, slot_b: int) -> bool:
    """Swap two items in a character's inventory.
    
    Returns True if both slots had items and were swapped, False if either was empty.
    """
    con = _connect(path)
    try:
        row_a = con.execute(
            "SELECT * FROM character_items WHERE character_guid = ? AND slot = ?",
            (character_guid, slot_a),
        ).fetchone()
        row_b = con.execute(
            "SELECT * FROM character_items WHERE character_guid = ? AND slot = ?",
            (character_guid, slot_b),
        ).fetchone()
        
        if not row_a and not row_b:
            return False
        
        # Use a temporary slot (-1) to perform the swap
        if row_a:
            con.execute(
                "UPDATE character_items SET slot = -1 WHERE character_guid = ? AND slot = ?",
                (character_guid, slot_a),
            )
        if row_b:
            con.execute(
                "UPDATE character_items SET slot = ? WHERE character_guid = ? AND slot = ?",
                (slot_a, character_guid, slot_b),
            )
        if row_a:
            con.execute(
                "UPDATE character_items SET slot = ? WHERE character_guid = ? AND slot = -1",
                (slot_b, character_guid),
            )
        con.commit()
        return True
    finally:
        con.close()


def get_character_spells(path: str, character_guid: int) -> List[sqlite3.Row]:
    """Fetch all active spells known by a character."""
    con = _connect(path)
    con.row_factory = sqlite3.Row
    try:
        return con.execute(
            "SELECT spell FROM character_spells WHERE character_guid = ? AND active = 1",
            (character_guid,),
        ).fetchall()
    finally:
        con.close()


# ``SpellAttributes`` bits used to decide whether a spell belongs on the action
# bar.  0x40 is SPELL_ATTR0_PASSIVE (Dodge, Defense, weapon proficiencies) and
# 0x80 is SPELL_ATTR0_HIDDEN_CLIENTSIDE; neither can be cast, so the client
# refuses to put such an entry on a button.
_SPELL_ATTR0_PASSIVE = 0x00000040
_SPELL_ATTR0_HIDDEN_CLIENTSIDE = 0x00000080
_AUTO_ATTACK_SPELL = 6603


def get_character_action_spells(path: str, character_guid: int) -> List[int]:
    """Return the spells that belong on the initial action bar.

    ``Attack`` is always button 0, followed by the character's known,
    non-passive, non-hidden spells in spell-id order.  Passive skills such as
    Dodge or One-Handed Axes are still sent in ``SMSG_INITIAL_SPELLS`` (the
    spellbook shows them) but cannot be placed on an action button.
    """
    con = _connect(path)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            """SELECT cs.spell, COALESCE(st.attributes, 0) AS attributes
               FROM character_spells cs
               LEFT JOIN spell_templates st ON st.id = cs.spell
               WHERE cs.character_guid = ? AND cs.active = 1
               ORDER BY cs.spell""",
            (character_guid,),
        ).fetchall()
    finally:
        con.close()

    action_spells: List[int] = []
    has_attack = False
    for row in rows:
        spell_id = int(row["spell"])
        if spell_id == _AUTO_ATTACK_SPELL:
            has_attack = True
            continue
        attributes = int(row["attributes"] or 0)
        if attributes & (_SPELL_ATTR0_PASSIVE | _SPELL_ATTR0_HIDDEN_CLIENTSIDE):
            continue
        if spell_id not in action_spells:
            action_spells.append(spell_id)
    if has_attack:
        action_spells.insert(0, _AUTO_ATTACK_SPELL)
    return action_spells


# ``SMSG_ACTION_BUTTONS`` packs ``action | (type << 24)``.  0 is a spell and
# 128 a macro id.  The three default macros in ``playercreateinfo_action`` are
# the standard Attack macros; this server has no macro storage, so they become
# an ordinary Attack (6603) button.
_ACTION_BUTTON_SPELL = 0
_ACTION_BUTTON_MACRO = 128
_STARTER_ATTACK_MACROS = frozenset((4604, 41751))


def get_character_action_buttons(path: str, character_guid: int) -> dict:
    """Return ``{button: (action, type)}`` for the initial action bar.

    ``playercreateinfo_action`` is the authoritative source and matches the
    real 3.3.5a client: button 0 is Attack, button 1 the class's first ability,
    the next slots the racials, and 72+ the second bar page.  When that table
    has no row for the character (an un-imported database), fall back to Attack
    plus every known non-passive spell in spell-id order.
    """
    con = _connect(path)
    con.row_factory = sqlite3.Row
    try:
        char = con.execute(
            "SELECT race, class FROM characters WHERE guid = ?",
            (int(character_guid),),
        ).fetchone()
        rows = []
        if char is not None:
            try:
                rows = con.execute(
                    "SELECT button, action, type FROM playercreateinfo_action"
                    " WHERE race = ? AND class = ? ORDER BY button",
                    (int(char["race"]), int(char["class"])),
                ).fetchall()
            except sqlite3.Error:
                rows = []
    finally:
        con.close()

    buttons: dict = {}
    for row in rows:
        button = int(row["button"])
        action = int(row["action"])
        action_type = int(row["type"])
        if action_type == _ACTION_BUTTON_MACRO:
            if action in _STARTER_ATTACK_MACROS:
                buttons[button] = (_AUTO_ATTACK_SPELL, _ACTION_BUTTON_SPELL)
            continue
        buttons[button] = (action, action_type)
    if buttons:
        return buttons

    spells = get_character_action_spells(path, character_guid)
    return {
        index: (spell_id, _ACTION_BUTTON_SPELL)
        for index, spell_id in enumerate(spells)
    }
