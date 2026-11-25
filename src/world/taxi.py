"""Flight paths: taxi nodes, routes, known-node masks and their packets.

Data comes from three client DBCs that ``tools/fetch_client_data.py --dbc``
downloads into ``<client_data>/dbc``:

``TaxiNodes.dbc`` (24 fields)
    id, map, x, y, z, name (16 locales + flags), mount creature id for the
    Horde (index 0) and the Alliance (index 1).
``TaxiPath.dbc`` (4 fields)
    id, from node, to node, cost in copper.
``TaxiPathNode.dbc`` (11 fields)
    id, path id, index, map, x, y, z, flags, delay, arrival/departure events.

Names are not used: the client draws the flight map from its own copy of the
DBCs, the server only needs ids, positions and prices.

The flow follows TrinityCore 3.3.5 (``TaxiHandler.cpp``,
``FlightPathMovementGenerator``): the nearest node to a flight master is the
one the player discovers there; ``SMSG_SHOWTAXINODES`` carries the player's
known-node mask; the flight itself is one smooth (Catmull-Rom) spline at
``FLIGHT_SPEED`` yards per second sent as ``SMSG_MONSTER_MOVE`` for the
player, with ``UNIT_FLAG_TAXI_FLIGHT`` and the mount display set until it lands.
"""
from __future__ import annotations

import math
import os
import sqlite3
import struct
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from src.world import databases
from src.world.object_update import (
    MONSTER_MOVE_NORMAL,
    MONSTER_MOVE_STOP,
    _monster_move_header,
)

Vec3 = Tuple[float, float, float]

DBC_FILES = ("TaxiNodes.dbc", "TaxiPath.dbc", "TaxiPathNode.dbc")

NPC_FLAG_FLIGHTMASTER = 0x00002000
GOSSIP_OPTION_TAXIVENDOR = 4

# UNIT_FIELD_FLAGS set for the whole flight (TrinityCore 3.3.5).
UNIT_FLAG_REMOVE_CLIENT_CONTROL = 0x00000004
UNIT_FLAG_TAXI_FLIGHT = 0x00000100
FLIGHT_UNIT_FLAGS = UNIT_FLAG_REMOVE_CLIENT_CONTROL | UNIT_FLAG_TAXI_FLIGHT

# PLAYER_FLIGHT_SPEED in TrinityCore.
FLIGHT_SPEED = 32.0

# MoveSplineFlag (3.3.5): Walkmode | Flying | Catmullrom, as SetWalk + SetFly +
# SetSmooth produce for a taxi spline.
SPLINE_FLAG_WALKMODE = 0x00001000
SPLINE_FLAG_FLYING = 0x00002000
SPLINE_FLAG_CATMULLROM = 0x00040000
FLIGHT_SPLINE_FLAGS = SPLINE_FLAG_WALKMODE | SPLINE_FLAG_FLYING | SPLINE_FLAG_CATMULLROM

# TaxiPathNode flags.
PATH_NODE_TELEPORT = 0x1       # map change: the flight stops here

# 3.3.5 taxi mask: 14 uint32 words, one bit per node id (node 1 = bit 0).
TAXI_MASK_SIZE = 14

# ActivateTaxiReply
ERR_TAXIOK = 0
ERR_TAXIUNSPECIFIEDSERVERERROR = 1
ERR_TAXINOSUCHPATH = 2
ERR_TAXINOTENOUGHMONEY = 3
ERR_TAXITOOFARAWAY = 4
ERR_TAXINOVENDORNEARBY = 5
ERR_TAXINOTVISITED = 6
ERR_TAXIPLAYERBUSY = 7
ERR_TAXIPLAYERALREADYMOUNTED = 8
ERR_TAXIPLAYERSHAPESHIFTED = 9
ERR_TAXIPLAYERMOVING = 10
ERR_TAXISAMENODE = 11
ERR_TAXINOTSTANDING = 12

ALLIANCE_RACES = frozenset({1, 3, 4, 7, 11})

# PlayerTaxi::InitTaxiNodesForLevel: every character knows its capital (and
# its faction's Outland entry point) from the start.
RACE_START_NODES = {
    1: (2,),            # Human: Stormwind
    2: (23,),           # Orc: Orgrimmar
    3: (6,),            # Dwarf: Ironforge
    4: (26, 27),        # Night Elf: Auberdine, Rut'theran Village
    5: (11,),           # Undead: Undercity
    6: (22,),           # Tauren: Thunder Bluff
    7: (6,),            # Gnome: Ironforge
    8: (23,),           # Troll: Orgrimmar
    10: (82,),          # Blood Elf: Silvermoon City
    11: (94,),          # Draenei: The Exodar
}
ALLIANCE_OUTLAND_NODE = 100     # Honor Hold
HORDE_OUTLAND_NODE = 99         # Thrallmar
SHATTERED_SUN_NODE = 213        # level 68+
SHATTERED_SUN_LEVEL = 68


def is_alliance(race: int) -> bool:
    return int(race) in ALLIANCE_RACES


def starting_nodes(race: int, level: int = 1) -> List[int]:
    nodes = list(RACE_START_NODES.get(int(race), ()))
    nodes.append(ALLIANCE_OUTLAND_NODE if is_alliance(race) else HORDE_OUTLAND_NODE)
    if int(level) >= SHATTERED_SUN_LEVEL:
        nodes.append(SHATTERED_SUN_NODE)
    return nodes


# ---------------------------------------------------------------------------
# DBC reading
# ---------------------------------------------------------------------------
def read_dbc(path: str, fmt: str) -> List[tuple]:
    """Records of a ``WDBC`` file unpacked with the struct format ``fmt``."""
    with open(path, "rb") as handle:
        data = handle.read()
    if len(data) < 20 or data[:4] != b"WDBC":
        raise ValueError(f"{path}: not a WDBC file")
    records, fields, size, _strings = struct.unpack_from("<4I", data, 4)
    if struct.calcsize("<" + fmt) != size:
        raise ValueError(f"{path}: record size {size} does not match {fmt!r}")
    return [struct.unpack_from("<" + fmt, data, 20 + i * size) for i in range(records)]


@dataclass(frozen=True)
class TaxiNode:
    id: int
    map: int
    x: float
    y: float
    z: float
    horde_mount: int
    alliance_mount: int

    def mount_for(self, alliance: bool) -> int:
        """The team's mount creature, else the other team's (TrinityCore allows it)."""
        primary = self.alliance_mount if alliance else self.horde_mount
        return primary or (self.horde_mount if alliance else self.alliance_mount)


@dataclass(frozen=True)
class TaxiPath:
    id: int
    source: int
    destination: int
    cost: int


@dataclass(frozen=True)
class PathNode:
    map: int
    x: float
    y: float
    z: float
    flags: int
    delay: int


@dataclass
class Route:
    nodes: List[int]
    cost: int
    points: List[Vec3]          # spline points after the start position
    mount_entry: int


@dataclass
class TaxiData:
    nodes: Dict[int, TaxiNode] = field(default_factory=dict)
    paths: Dict[Tuple[int, int], TaxiPath] = field(default_factory=dict)
    path_nodes: Dict[int, List[PathNode]] = field(default_factory=dict)

    @classmethod
    def load(cls, dbc_dir: str) -> "TaxiData":
        nodes_raw = read_dbc(os.path.join(dbc_dir, "TaxiNodes.dbc"), "II3f17I2I")
        paths_raw = read_dbc(os.path.join(dbc_dir, "TaxiPath.dbc"), "4I")
        points_raw = read_dbc(os.path.join(dbc_dir, "TaxiPathNode.dbc"), "4I3f4I")
        data = cls()
        for row in nodes_raw:
            data.nodes[row[0]] = TaxiNode(row[0], row[1], row[2], row[3], row[4],
                                          row[22], row[23])
        for row in paths_raw:
            data.paths[(row[1], row[2])] = TaxiPath(row[0], row[1], row[2], row[3])
        indexed: Dict[int, List[Tuple[int, PathNode]]] = {}
        for row in points_raw:
            indexed.setdefault(row[1], []).append(
                (row[2], PathNode(row[3], row[4], row[5], row[6], row[7], row[8])))
        for path_id, items in indexed.items():
            items.sort(key=lambda item: item[0])
            data.path_nodes[path_id] = [node for _index, node in items]
        return data

    def _usable(self, node: TaxiNode) -> bool:
        return bool(node.horde_mount or node.alliance_mount)

    def nearest_node(self, map_id: int, x: float, y: float, z: float,
                     alliance: bool) -> int:
        """``ObjectMgr::GetNearestTaxiNode``: 0 when the map has none for the team."""
        best, best_dist = 0, None
        for node in self.nodes.values():
            if node.map != int(map_id) or not self._usable(node):
                continue
            if not (node.alliance_mount if alliance else node.horde_mount):
                continue
            dist = (node.x - x) ** 2 + (node.y - y) ** 2 + (node.z - z) ** 2
            if best_dist is None or dist < best_dist:
                best, best_dist = node.id, dist
        return best

    def route(self, nodes: Sequence[int], alliance: bool) -> Optional[Route]:
        """Spline, cost and mount for flying through ``nodes`` (None = no path)."""
        nodes = [int(n) for n in nodes]
        if len(nodes) < 2 or any(n not in self.nodes for n in nodes):
            return None
        start = self.nodes[nodes[0]]
        mount = start.mount_for(alliance)
        if not mount:
            return None
        cost = 0
        points: List[Vec3] = []
        for source, destination in zip(nodes, nodes[1:]):
            path = self.paths.get((source, destination))
            if path is None:
                return None
            cost += path.cost
            for node in self.path_nodes.get(path.id, []):
                if node.map != start.map:
                    break
                points.append((node.x, node.y, node.z))
                if node.flags & PATH_NODE_TELEPORT:
                    break
        # The first point is the flight master's own node, which the spline
        # replaces with the player's real position (MoveSplineInit::Launch).
        points = _dedupe(points[1:])
        if not points:
            return None
        return Route(nodes, cost, points, mount)


def _dedupe(points: Iterable[Vec3]) -> List[Vec3]:
    result: List[Vec3] = []
    for point in points:
        if result and math.dist(result[-1], point) < 0.1:
            continue
        result.append(point)
    return result


def find_dbc_dir(client_data: str) -> Optional[str]:
    if not client_data:
        return None
    path = os.path.join(client_data, "dbc")
    if all(os.path.isfile(os.path.join(path, name)) for name in DBC_FILES):
        return path
    return None


_CACHE: Dict[str, Optional[TaxiData]] = {}


def get_taxi_data(client_data: str) -> Optional[TaxiData]:
    """Load (once) the taxi DBCs below ``client_data``; None when missing."""
    key = os.path.abspath(client_data) if client_data else ""
    if key not in _CACHE:
        dbc_dir = find_dbc_dir(client_data)
        _CACHE[key] = TaxiData.load(dbc_dir) if dbc_dir else None
    return _CACHE[key]


def clear_cache() -> None:
    _CACHE.clear()


# ---------------------------------------------------------------------------
# Spline timing (TrinityCore Movement::Spline, Catmull-Rom mode)
# ---------------------------------------------------------------------------
_STEPS_PER_SEGMENT = 3


def _catmull_rom(p0: Vec3, p1: Vec3, p2: Vec3, p3: Vec3, t: float) -> Vec3:
    t2, t3 = t * t, t * t * t
    return tuple(
        0.5 * ((2.0 * b) + (-a + c) * t + (2.0 * a - 5.0 * b + 4.0 * c - d) * t2
               + (-a + 3.0 * b - 3.0 * c + d) * t3)
        for a, b, c, d in zip(p0, p1, p2, p3)
    )


def spline_length(start: Vec3, points: Sequence[Vec3]) -> float:
    """Length of the smooth spline ``start -> points`` as the server measures it."""
    controls = [tuple(start)] + [tuple(p) for p in points]
    if len(controls) < 2:
        return 0.0
    padded = [controls[0]] + controls + [controls[-1]]
    length = 0.0
    for i in range(len(controls) - 1):
        p0, p1, p2, p3 = padded[i], padded[i + 1], padded[i + 2], padded[i + 3]
        current = p1
        for step in range(1, _STEPS_PER_SEGMENT + 1):
            nxt = _catmull_rom(p0, p1, p2, p3, step / _STEPS_PER_SEGMENT)
            length += math.dist(current, nxt)
            current = nxt
    return length


def flight_duration_ms(start: Vec3, points: Sequence[Vec3],
                       speed: float = FLIGHT_SPEED) -> int:
    return max(1, int(spline_length(start, points) / speed * 1000.0))


class FlightTrack:
    """Where a flying player is at a given time (polyline approximation)."""

    def __init__(self, start: Vec3, points: Sequence[Vec3], duration_ms: int):
        self.vertices: List[Vec3] = [tuple(start)] + [tuple(p) for p in points]
        self.duration = max(0.001, duration_ms / 1000.0)
        self.lengths: List[float] = [0.0]
        for a, b in zip(self.vertices, self.vertices[1:]):
            self.lengths.append(self.lengths[-1] + math.dist(a, b))

    @property
    def destination(self) -> Vec3:
        return self.vertices[-1]

    def position(self, elapsed: float) -> Vec3:
        fraction = min(max(elapsed / self.duration, 0.0), 1.0)
        target = fraction * self.lengths[-1]
        for i in range(1, len(self.lengths)):
            if self.lengths[i] >= target:
                span = self.lengths[i] - self.lengths[i - 1]
                local = 0.0 if span <= 0 else (target - self.lengths[i - 1]) / span
                a, b = self.vertices[i - 1], self.vertices[i]
                return tuple(a[k] + (b[k] - a[k]) * local for k in range(3))
        return self.destination


# ---------------------------------------------------------------------------
# Known nodes
# ---------------------------------------------------------------------------
def build_taxi_mask(nodes: Iterable[int]) -> List[int]:
    words = [0] * TAXI_MASK_SIZE
    for node in nodes:
        node = int(node)
        if node <= 0:
            continue
        index, bit = divmod(node - 1, 32)
        if index < TAXI_MASK_SIZE:
            words[index] |= 1 << bit
    return words


_SCHEMA = """
CREATE TABLE IF NOT EXISTS character_taxi_nodes(
  guid INTEGER NOT NULL,
  node INTEGER NOT NULL,
  PRIMARY KEY (guid, node)
)
"""


def ensure_schema(con: sqlite3.Connection) -> None:
    # character_db creates the table with the rest of the character store;
    # this keeps standalone test/tool databases working too.
    con.execute(_SCHEMA)


def load_known_nodes(path: str, guid: int, race: int, level: int) -> set:
    """The character's discovered nodes; seeded with its race's defaults.

    ``path`` is the world path the server passes around; the connection is
    routed to characters.db when the databases are split.  The race defaults
    are written on first load, so call it through ``run_db_write``.
    """
    con = databases.connect_character(path)
    try:
        ensure_schema(con)
        rows = con.execute(
            "SELECT node FROM character_taxi_nodes WHERE guid = ?", (int(guid),)
        ).fetchall()
        known = {int(row[0]) for row in rows}
        missing = [n for n in starting_nodes(race, level) if n not in known]
        if missing:
            con.executemany(
                "INSERT OR IGNORE INTO character_taxi_nodes(guid, node) VALUES(?, ?)",
                [(int(guid), n) for n in missing])
            con.commit()
            known.update(missing)
        return known
    finally:
        con.close()


def save_known_node(path: str, guid: int, node: int) -> None:
    con = databases.connect_character(path)
    try:
        ensure_schema(con)
        con.execute(
            "INSERT OR IGNORE INTO character_taxi_nodes(guid, node) VALUES(?, ?)",
            (int(guid), int(node)))
        con.commit()
    finally:
        con.close()


# ---------------------------------------------------------------------------
# Packets
# ---------------------------------------------------------------------------
def build_show_taxi_nodes(npc_guid: int, current_node: int,
                          known: Iterable[int]) -> bytes:
    """``SMSG_SHOWTAXINODES``: uint32 1, npc guid, current node, 14-word mask."""
    out = struct.pack("<IQI", 1, int(npc_guid), int(current_node))
    out += struct.pack(f"<{TAXI_MASK_SIZE}I", *build_taxi_mask(known))
    return out


def build_taxinode_status(npc_guid: int, known: bool) -> bytes:
    return struct.pack("<QB", int(npc_guid), 1 if known else 0)


def build_activate_taxi_reply(error: int) -> bytes:
    return struct.pack("<I", int(error))


def parse_activate_taxi(data: bytes) -> Optional[Tuple[int, List[int]]]:
    """``CMSG_ACTIVATETAXI``: npc guid, source node, destination node."""
    if len(data) < 16:
        return None
    guid, source, destination = struct.unpack_from("<QII", data, 0)
    return guid, [source, destination]


def parse_activate_taxi_express(data: bytes) -> Optional[Tuple[int, List[int]]]:
    """``CMSG_ACTIVATETAXIEXPRESS``: npc guid, uint32 count, count node ids."""
    if len(data) < 12:
        return None
    guid, count = struct.unpack_from("<QI", data, 0)
    if count < 2 or len(data) < 12 + 4 * count:
        return None
    return guid, list(struct.unpack_from(f"<{count}I", data, 12))


def build_flight_move(guid: int, start: Vec3, points: Sequence[Vec3],
                      duration_ms: int, move_id: int = 0) -> bytes:
    """``SMSG_MONSTER_MOVE`` for a taxi flight (Catmull-Rom path).

    ``PacketBuilder::WriteCatmullRomPath`` writes the point count followed by
    every point after the start as a plain Vector3 (no packed offsets).
    """
    out = _monster_move_header(guid, start[0], start[1], start[2], move_id,
                               MONSTER_MOVE_NORMAL)
    out += struct.pack("<I", FLIGHT_SPLINE_FLAGS)
    out += struct.pack("<I", max(1, int(duration_ms)))
    out += struct.pack("<I", len(points))
    for point in points:
        out += struct.pack("<fff", float(point[0]), float(point[1]), float(point[2]))
    return bytes(out)


def build_move_stop(guid: int, position: Vec3, move_id: int = 0) -> bytes:
    """``SMSG_MONSTER_MOVE`` with ``MonsterMoveStop``: the spline ends here."""
    return bytes(_monster_move_header(guid, position[0], position[1], position[2],
                                      move_id, MONSTER_MOVE_STOP))
