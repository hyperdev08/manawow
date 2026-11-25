"""Nearest-graveyard selection and corpse/death persistence."""
from __future__ import annotations

import sqlite3

from src.world import character_db, graveyard


def _seed(path: str) -> None:
    graveyard.init_graveyard_db(path)
    con = sqlite3.connect(path)
    try:
        con.executemany(
            "INSERT INTO graveyard_locations(id, map, x, y, z, o, name)"
            " VALUES(?,?,?,?,?,?,?)",
            [
                (1, 0, 0.0, 0.0, 10.0, 0.0, "Zone/team"),
                (2, 0, 100.0, 0.0, 10.0, 0.0, "Zone/both"),
                (3, 0, 50.0, 0.0, 10.0, 0.0, "Other team"),
                (4, 1, 0.0, 0.0, 10.0, 0.0, "Dungeon fallback"),
                (5, 0, 0.0, 0.0, 10.0, 0.0, "Enemy-only zone"),
            ],
        )
        con.executemany(
            "INSERT INTO graveyard_zones(id, ghost_loc, link_kind, faction)"
            " VALUES(?,?,?,?)",
            [
                (1, 12, 0, graveyard.TEAM_ALLIANCE),
                (2, 12, 0, 0),
                (3, 12, 0, graveyard.TEAM_HORDE),
                (4, 999, 0, graveyard.TEAM_ALLIANCE),
                (5, 13, 0, graveyard.TEAM_HORDE),
            ],
        )
        con.commit()
    finally:
        con.close()


def test_zone_and_team_win_over_distance(tmp_path):
    path = str(tmp_path / "graves.db")
    _seed(path)
    # The Alliance graveyard is farther than the Horde one but its rank wins.
    grave = graveyard.closest_graveyard(
        path, 0, 12, 49.0, 0.0, 10.0, race=1)
    assert grave is not None and grave["id"] == 1
    grave = graveyard.closest_graveyard(
        path, 0, 12, 49.0, 0.0, 10.0, race=2)
    assert grave is not None and grave["id"] == 3


def test_enemy_faction_graveyard_is_never_selected(tmp_path):
    path = str(tmp_path / "graves.db")
    _seed(path)
    # Zone 13 only links the Horde graveyard; an Alliance ghost must fall back
    # to its same-map team graveyard instead of the enemy one.
    grave = graveyard.closest_graveyard(
        path, 0, 13, 0.0, 0.0, 10.0, race=1)
    assert grave is not None and grave["id"] != 5


def test_falls_back_to_same_map_then_other_map(tmp_path):
    path = str(tmp_path / "graves.db")
    _seed(path)
    grave = graveyard.closest_graveyard(
        path, 0, 999, 0.0, 0.0, 0.0, race=1)
    # Zone 999 only has the dungeon entry, which is on another map.
    assert grave is not None and grave["id"] == 4
    grave = graveyard.closest_graveyard(
        path, 0, 77, 90.0, 0.0, 0.0, race=1)
    # No zone link: the same-map team graveyard beats the neutral one.
    assert grave is not None and grave["id"] == 1


def test_zone_links_win_outright_over_map_links(tmp_path):
    path = str(tmp_path / "graves.db")
    graveyard.init_graveyard_db(path)
    con = sqlite3.connect(path)
    try:
        con.executemany(
            "INSERT INTO graveyard_locations(id, map, x, y, z, o, name)"
            " VALUES(?,?,?,?,?,?,?)",
            [
                (10, 0, 0.0, 0.0, 10.0, 0.0, "Zone link"),
                (11, 0, 100.0, 0.0, 10.0, 0.0, "Map link"),
            ],
        )
        con.executemany(
            "INSERT INTO graveyard_zones(id, ghost_loc, link_kind, faction)"
            " VALUES(?,?,?,?)",
            [
                (10, 12, graveyard.GRAVEYARD_AREALINK, 0),
                (11, 0, graveyard.GRAVEYARD_MAPLINK, 0),
            ],
        )
        con.commit()
    finally:
        con.close()
    # The zone link wins even though the map link is at the death position.
    grave = graveyard.closest_graveyard(path, 0, 12, 100.0, 0.0, 10.0, race=1)
    assert grave is not None and grave["id"] == 10
    # With no zone link, only the MAPLINK matches the map.
    grave = graveyard.closest_graveyard(path, 0, 77, 100.0, 0.0, 10.0, race=1)
    assert grave is not None and grave["id"] == 11


def test_graveyard_schema_includes_link_kind_in_the_primary_key(tmp_path):
    path = str(tmp_path / "graves.db")
    con = sqlite3.connect(path)
    try:
        con.executescript(
            """
            CREATE TABLE graveyard_zones(
              id INTEGER NOT NULL,
              ghost_loc INTEGER NOT NULL,
              link_kind INTEGER NOT NULL DEFAULT 0,
              faction INTEGER NOT NULL DEFAULT 0,
              PRIMARY KEY(id, ghost_loc, faction)
            );
            INSERT INTO graveyard_zones VALUES(1, 12, 0, 0);
            """)
        con.commit()
    finally:
        con.close()
    graveyard.init_graveyard_db(path)
    con = sqlite3.connect(path)
    try:
        con.execute(
            "INSERT INTO graveyard_zones(id, ghost_loc, link_kind, faction)"
            " VALUES(1, 12, 1, 0)")
        con.commit()
        assert con.execute(
            "SELECT COUNT(*) FROM graveyard_zones").fetchone()[0] == 2
        pk = [row[1] for row in sorted(
            con.execute("PRAGMA table_info(graveyard_zones)"),
            key=lambda row: row[5]) if row[5]]
        assert "link_kind" in pk
    finally:
        con.close()


def test_char_enum_marks_ghost_players():
    import struct

    char = {
        "guid": 7, "name": "Ghost", "race": 1, "class": 1, "gender": 0,
        "skin": 0, "face": 0, "hair_style": 0, "hair_color": 0,
        "facial_style": 0, "level": 1, "zone": 12, "map": 0,
        "x": 0.0, "y": 0.0, "z": 0.0, "guild_id": 0, "char_flags": 0,
        "customize_flags": 0, "first_login": 0, "pet_display_id": 0,
        "pet_level": 0, "pet_family": 0, "death_state": 2,
    }
    payload = character_db.build_char_enum_payload([char])
    # count(1) + guid(8) + name(6) + appearance(9) + zone(4) + map(4)
    # + xyz(12) + guild(4) ===> char flags
    offset = 1 + 8 + len("Ghost\x00") + 9 + 4 + 4 + 12 + 4
    flags = struct.unpack_from("<I", payload, offset)[0]
    assert flags & character_db.CHARACTER_FLAG_GHOST_CHAR_ENUM
    assert flags & 0x2000


def test_corpse_and_death_state_round_trip(tmp_path):
    path = str(tmp_path / "characters.db")
    character_db.init_character_db(path)
    guid = character_db.create_character(
        path, 1, "Ghostguy", 1, 1, 0, 0, 0, 0, 0, 0)
    character_db.save_character_corpse(
        path, guid, 0, 12, -100.0, -200.0, 80.0, 1.5)
    corpse = character_db.get_character_corpse(path, guid)
    assert corpse is not None
    assert (corpse["map"], corpse["zone"], corpse["x"]) == (0, 12, -100.0)
    assert corpse["released"] == 0
    character_db.mark_character_corpse_released(path, guid, 1234)
    assert character_db.get_character_corpse(path, guid)["released"] == 1234
    character_db.set_character_death_state(path, guid, 2)
    row = character_db.get_character(path, 1, guid)
    assert int(row["death_state"]) == 2
    character_db.delete_character_corpse(path, guid)
    assert character_db.get_character_corpse(path, guid) is None
    assert character_db.delete_character(path, 1, guid)
    assert character_db.get_character_corpse(path, guid) is None
