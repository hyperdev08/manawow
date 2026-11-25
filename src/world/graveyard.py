"""Graveyard locations and the nearest-graveyard lookup (3.3.5a).

CMaNGOS splits the data in two: ``world_safe_locs`` holds the actual map and
coordinates (also used by taxi paths), while ``game_graveyard_zone`` links a
graveyard to the zone whose ghosts may use it and to a team (0 = both).  The
importer fills both tables; this module only reads them.
"""
from __future__ import annotations

import math
import sqlite3
from typing import Mapping, Optional

from src.common import sqlite_tuning

CREATE_GRAVEYARD_SQL = """
CREATE TABLE IF NOT EXISTS graveyard_locations(
  id INTEGER PRIMARY KEY,
  map INTEGER NOT NULL DEFAULT 0,
  x REAL NOT NULL DEFAULT 0.0,
  y REAL NOT NULL DEFAULT 0.0,
  z REAL NOT NULL DEFAULT 0.0,
  o REAL NOT NULL DEFAULT 0.0,
  name TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS graveyard_zones(
  id INTEGER NOT NULL,
  ghost_loc INTEGER NOT NULL,
  link_kind INTEGER NOT NULL DEFAULT 0,
  faction INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(id, ghost_loc, link_kind, faction)
);
CREATE INDEX IF NOT EXISTS idx_graveyard_zone_loc
  ON graveyard_zones(ghost_loc);
"""

# CMaNGOS ``GraveyardLinkKey`` kinds.  AREALINK rows compare ``ghost_loc``
# against the player's zone; MAPLINK rows compare it against the map id.
GRAVEYARD_AREALINK = 0
GRAVEYARD_MAPLINK = 1

# ``game_graveyard_zone.faction`` uses the team mask values, not race ids.
TEAM_ALLIANCE = 469
TEAM_HORDE = 67

ALLIANCE_RACES = frozenset((1, 3, 4, 7, 11))
HORDE_RACES = frozenset((2, 5, 6, 8, 10))


def ensure_graveyard_schema(con: sqlite3.Connection) -> None:
    """Create the graveyard tables and upgrade the old zone-link primary key."""
    con.executescript(CREATE_GRAVEYARD_SQL)
    columns = con.execute("PRAGMA table_info(graveyard_zones)").fetchall()
    if not columns:
        return
    if "link_kind" not in {row[1] for row in columns}:
        con.execute(
            "ALTER TABLE graveyard_zones ADD COLUMN link_kind INTEGER"
            " NOT NULL DEFAULT 0")
        columns = con.execute(
            "PRAGMA table_info(graveyard_zones)").fetchall()
    # PRAGMA table_info column 5 is the 1-based position in the primary key.
    primary_key = [row[1] for row in sorted(columns, key=lambda row: row[5])
                   if row[5]]
    if "link_kind" in primary_key:
        return
    # Older databases keyed links by (id, ghost_loc, faction), which cannot
    # store the same zone/map link for both link kinds.  SQLite cannot alter a
    # primary key, so rebuild the table once.
    con.executescript(
        """
        DROP TABLE IF EXISTS graveyard_zones_legacy;
        ALTER TABLE graveyard_zones RENAME TO graveyard_zones_legacy;
        CREATE TABLE graveyard_zones(
          id INTEGER NOT NULL,
          ghost_loc INTEGER NOT NULL,
          link_kind INTEGER NOT NULL DEFAULT 0,
          faction INTEGER NOT NULL DEFAULT 0,
          PRIMARY KEY(id, ghost_loc, link_kind, faction)
        );
        INSERT OR IGNORE INTO graveyard_zones(
            id, ghost_loc, link_kind, faction)
          SELECT id, ghost_loc, link_kind, faction
          FROM graveyard_zones_legacy;
        DROP TABLE graveyard_zones_legacy;
        CREATE INDEX IF NOT EXISTS idx_graveyard_zone_loc
          ON graveyard_zones(ghost_loc);
        """)


def init_graveyard_db(path: str) -> None:
    con = sqlite3.connect(path)
    try:
        ensure_graveyard_schema(con)
        con.commit()
    finally:
        con.close()


def team_for_race(race: int) -> int:
    race = int(race)
    if race in ALLIANCE_RACES:
        return TEAM_ALLIANCE
    if race in HORDE_RACES:
        return TEAM_HORDE
    return 0


def closest_graveyard(path: str, map_id: int, zone: int, x: float, y: float,
                      z: float, race: int) -> Optional[dict]:
    """Return the closest usable graveyard for a dead player.

    Ranking follows CMaNGOS ``Player::GetClosestGraveYard``: an exact zone and
    team link wins, then a zone link open to both teams, then any graveyard on
    the same map, then the nearest cross-map fallback (instances).
    """
    team = team_for_race(race)
    map_id, zone = int(map_id), int(zone)
    try:
        con = sqlite_tuning.read_connection(path)
        # Keep every linked graveyard: a dungeon's spirit healer is often on
        # another map, and the zone/team link must win over same-map distance.
        rows = con.execute(
            "SELECT l.id, l.map, l.x, l.y, l.z, l.o, l.name,"
            " z.ghost_loc, z.faction, z.link_kind"
            " FROM graveyard_locations l"
            " JOIN graveyard_zones z ON z.id = l.id",
        ).fetchall()
    except sqlite3.Error:
        return None
    if not rows:
        return None

    def distance(row) -> float:
        dx = float(row[2]) - float(x)
        dy = float(row[3]) - float(y)
        dz = float(row[4]) - float(z)
        return dx * dx + dy * dy + dz * dz

    best = None
    best_rank = None
    for row in rows:
        ghost_loc = int(row[7] or 0)
        faction = int(row[8] or 0)
        link_kind = int(row[9] or 0)
        # CMaNGOS ignores link kinds it does not know.
        if link_kind not in (GRAVEYARD_AREALINK, GRAVEYARD_MAPLINK):
            continue
        # A graveyard explicitly linked to the enemy team must never be used,
        # even when it is the closest one on the map/instance.
        if faction and faction != team:
            continue
        is_zone_link = (link_kind == GRAVEYARD_AREALINK
                        and zone and ghost_loc == zone)
        is_map_link = (link_kind == GRAVEYARD_MAPLINK
                       and ghost_loc == map_id)
        if is_zone_link and faction == team:
            rank = 0
        elif is_zone_link and faction == 0:
            rank = 1
        elif is_map_link and faction == team:
            rank = 2
        elif is_map_link and faction == 0:
            rank = 3
        # Upstream falls back to the team's default graveyard when no link
        # matches.  Keep a same-map/cross-map geometric fallback for custom
        # data sets that contain no usable links at all.
        elif int(row[1]) == map_id and faction == team:
            rank = 4
        elif int(row[1]) == map_id and faction == 0:
            rank = 5
        elif faction == team:
            rank = 6
        else:
            rank = 7
        dist = distance(row)
        if (best is None or rank < best_rank
                or (rank == best_rank and dist < best[0])):
            best = (dist, row)
            best_rank = rank
    row = best[1]
    link_kind = int(row[9] or 0)
    return {
        "id": int(row[0]),
        "map": int(row[1]),
        "x": float(row[2]),
        "y": float(row[3]),
        "z": float(row[4]),
        "o": float(row[5]),
        "name": str(row[6] or ""),
        # A MAPLINK stores the map id in ``ghost_loc``; it is not a zone.
        "zone": int(row[7] or 0) if link_kind == GRAVEYARD_AREALINK else 0,
    }


def corpse_distance(a: Mapping, b: Mapping) -> float:
    return math.dist((float(a["x"]), float(a["y"]), float(a["z"])),
                     (float(b["x"]), float(b["y"]), float(b["z"])))
