"""Static game objects: templates, spawns, create blocks and queries.

Game objects are everything in the world that is not a unit: doors, chests,
herbs, mining nodes, signposts, benches, mailboxes, quest objects.  The tables
mirror the parts of the CMaNGOS ``gameobject_template`` / ``gameobject`` /
``gameobject_addon`` tables the client needs to draw an object:

``gameobject_templates``
    One row per entry: type, display model, name, flags, faction, size and the
    24 type-specific ``data`` fields the client reads from
    ``SMSG_GAMEOBJECT_QUERY_RESPONSE`` (a door's auto-close time, a chest's
    lock id, a goober's quest id...).

``gameobject_spawns``
    One row per placed object with its world rotation (quaternion), respawn
    time and the ``gameobject_addon`` state/animation overrides.

Unlike creatures, the spawns never change at runtime, so each map is read once
(off the event loop) into an in-memory grid and every visibility scan is a
dictionary lookup instead of a database query.  That keeps the per-heartbeat
cost independent of disk speed.
"""
from __future__ import annotations

import math
import sqlite3
import struct
import threading
import zlib
from typing import Dict, Iterable, List, Mapping, Optional, Tuple

from src.protocol.opcodes import Opcodes
from src.world import update_fields as F

# ---------------------------------------------------------------------------
# Guids
# ---------------------------------------------------------------------------
# TrinityCore/CMaNGOS ``HIGHGUID_GAMEOBJECT``.  Like creatures, the template
# entry sits in bits 24-47 so the client knows which entry to query.
GAMEOBJECT_HIGH_GUID = 0xF110000000000000
GAMEOBJECT_GUID_ENTRY_SHIFT = 24
GAMEOBJECT_GUID_ENTRY_MASK = 0xFFFFFF
GAMEOBJECT_GUID_COUNTER_MASK = 0xFFFFFF


def gameobject_guid(low_guid: int, entry: int) -> int:
    """Return the 64-bit object guid for a game object spawn."""
    return (
        GAMEOBJECT_HIGH_GUID
        | ((int(entry) & GAMEOBJECT_GUID_ENTRY_MASK) << GAMEOBJECT_GUID_ENTRY_SHIFT)
        | (int(low_guid) & GAMEOBJECT_GUID_COUNTER_MASK)
    )


def gameobject_low_guid(guid: int) -> int:
    return int(guid) & GAMEOBJECT_GUID_COUNTER_MASK


def gameobject_entry_from_guid(guid: int) -> int:
    return (int(guid) >> GAMEOBJECT_GUID_ENTRY_SHIFT) & GAMEOBJECT_GUID_ENTRY_MASK


def is_gameobject_guid(guid: int) -> bool:
    return (int(guid) >> 48) == (GAMEOBJECT_HIGH_GUID >> 48)


# ---------------------------------------------------------------------------
# Types, states and update constants
# ---------------------------------------------------------------------------
GAMEOBJECT_TYPE_DOOR = 0
GAMEOBJECT_TYPE_BUTTON = 1
GAMEOBJECT_TYPE_QUESTGIVER = 2
GAMEOBJECT_TYPE_CHEST = 3
GAMEOBJECT_TYPE_GENERIC = 5
GAMEOBJECT_TYPE_TRAP = 6
GAMEOBJECT_TYPE_GOOBER = 10
GAMEOBJECT_TYPE_TRANSPORT = 11
GAMEOBJECT_TYPE_MO_TRANSPORT = 15
GAMEOBJECT_TYPE_DUEL_ARBITER = 16
GAMEOBJECT_TYPE_FLAGSTAND = 24
GAMEOBJECT_TYPE_FLAGDROP = 26

# Elevators and boats move on a client-side path driven by
# ``UPDATEFLAG_TRANSPORT`` and the server's transport clock; drawn as static
# objects they would hang in mid-air, so they are left out until transports
# exist.
UNSUPPORTED_TYPES = frozenset({GAMEOBJECT_TYPE_TRANSPORT, GAMEOBJECT_TYPE_MO_TRANSPORT})

GO_STATE_ACTIVE = 0            # open door, used chest
GO_STATE_READY = 1             # closed door, untouched object
GO_STATE_ACTIVE_ALTERNATIVE = 2

# CMaNGOS ``GO_ANIMPROGRESS_DEFAULT``: used when a spawn has no addon row.
GO_ANIMPROGRESS_DEFAULT = 0xFF

TYPEID_GAMEOBJECT = 5
UPDATETYPE_CREATE_OBJECT = 2
UPDATETYPE_CREATE_OBJECT2 = 3
# TrinityCore names (CMaNGOS calls 0x10 HIGHGUID and 0x40 HAS_POSITION).
UPDATEFLAG_LOWGUID = 0x0010
UPDATEFLAG_STATIONARY_POSITION = 0x0040
UPDATEFLAG_ROTATION = 0x0200
GAMEOBJECT_UPDATE_FLAGS = (UPDATEFLAG_LOWGUID | UPDATEFLAG_STATIONARY_POSITION
                           | UPDATEFLAG_ROTATION)

# The client's quest-item list in the query response is fixed at six slots and
# the type-specific data at 24.
MAX_GAMEOBJECT_DATA = 24
MAX_GAMEOBJECT_QUEST_ITEMS = 6

# Default distance at which objects are sent; same neighbourhood as creatures.
GAMEOBJECT_VISIBILITY_RADIUS = 150.0
GAMEOBJECT_RESCAN_DISTANCE = 25.0
_CELL_SIZE = 64.0

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------
_DATA_COLUMNS = ",\n".join(
    f"  data{i} INTEGER NOT NULL DEFAULT 0" for i in range(MAX_GAMEOBJECT_DATA)
)
_QUEST_ITEM_COLUMNS = ",\n".join(
    f"  quest_item{i} INTEGER NOT NULL DEFAULT 0"
    for i in range(1, MAX_GAMEOBJECT_QUEST_ITEMS + 1)
)

CREATE_GAMEOBJECT_SQL = f"""
CREATE TABLE IF NOT EXISTS gameobject_templates(
  entry INTEGER PRIMARY KEY,
  type INTEGER NOT NULL DEFAULT 0,
  display_id INTEGER NOT NULL DEFAULT 0,
  name TEXT NOT NULL DEFAULT '',
  icon_name TEXT NOT NULL DEFAULT '',
  cast_bar_caption TEXT NOT NULL DEFAULT '',
  unk1 TEXT NOT NULL DEFAULT '',
  faction INTEGER NOT NULL DEFAULT 0,
  flags INTEGER NOT NULL DEFAULT 0,
  extra_flags INTEGER NOT NULL DEFAULT 0,
  size REAL NOT NULL DEFAULT 1.0,
{_QUEST_ITEM_COLUMNS},
{_DATA_COLUMNS},
  min_gold INTEGER NOT NULL DEFAULT 0,
  max_gold INTEGER NOT NULL DEFAULT 0,
  script_name TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS gameobject_spawns(
  guid INTEGER PRIMARY KEY,
  entry INTEGER NOT NULL,
  map INTEGER NOT NULL,
  phase_mask INTEGER NOT NULL DEFAULT 1,
  x REAL NOT NULL, y REAL NOT NULL, z REAL NOT NULL, o REAL NOT NULL DEFAULT 0,
  rotation0 REAL NOT NULL DEFAULT 0, rotation1 REAL NOT NULL DEFAULT 0,
  rotation2 REAL NOT NULL DEFAULT 0, rotation3 REAL NOT NULL DEFAULT 0,
  spawn_time_min INTEGER NOT NULL DEFAULT 0,
  spawn_time_max INTEGER NOT NULL DEFAULT 0,
  anim_progress INTEGER NOT NULL DEFAULT {GO_ANIMPROGRESS_DEFAULT},
  state INTEGER NOT NULL DEFAULT -1
);
CREATE INDEX IF NOT EXISTS idx_gameobject_spawns_map ON gameobject_spawns(map);

-- CMaNGOS ``game_event_gameobject``: a positive event spawns the object only
-- while that world event (Winter Veil, Hallow's End...) runs, a negative one
-- removes it during the event.  There is no event calendar yet, so spawns
-- with a positive event are never shown.
CREATE TABLE IF NOT EXISTS gameobject_events(
  guid INTEGER NOT NULL,
  event INTEGER NOT NULL,
  PRIMARY KEY(guid, event)
);
"""

TEMPLATE_COLUMNS: Tuple[str, ...] = (
    "entry", "type", "display_id", "name", "icon_name", "cast_bar_caption",
    "unk1", "faction", "flags", "extra_flags", "size",
    *(f"quest_item{i}" for i in range(1, MAX_GAMEOBJECT_QUEST_ITEMS + 1)),
    *(f"data{i}" for i in range(MAX_GAMEOBJECT_DATA)),
    "min_gold", "max_gold", "script_name",
)
SPAWN_COLUMNS: Tuple[str, ...] = (
    "guid", "entry", "map", "phase_mask", "x", "y", "z", "o",
    "rotation0", "rotation1", "rotation2", "rotation3",
    "spawn_time_min", "spawn_time_max", "anim_progress", "state",
)


def init_gameobject_db(path: str) -> None:
    con = sqlite3.connect(path)
    try:
        con.executescript(CREATE_GAMEOBJECT_SQL)
        con.commit()
    finally:
        con.close()
    clear_cache()


# ---------------------------------------------------------------------------
# Static data access
# ---------------------------------------------------------------------------
class _MapGrid:
    """All visible spawns of one map, bucketed into 64-yard cells."""

    __slots__ = ("cells", "count")

    def __init__(self, spawns: Iterable[dict]):
        self.cells: Dict[Tuple[int, int], List[dict]] = {}
        self.count = 0
        for spawn in spawns:
            key = (int(spawn["x"] // _CELL_SIZE), int(spawn["y"] // _CELL_SIZE))
            self.cells.setdefault(key, []).append(spawn)
            self.count += 1

    def near(self, x: float, y: float, z: float, radius: float) -> List[dict]:
        limit = radius * radius
        lo_x, hi_x = int((x - radius) // _CELL_SIZE), int((x + radius) // _CELL_SIZE)
        lo_y, hi_y = int((y - radius) // _CELL_SIZE), int((y + radius) // _CELL_SIZE)
        found = []
        for cx in range(lo_x, hi_x + 1):
            for cy in range(lo_y, hi_y + 1):
                for spawn in self.cells.get((cx, cy), ()):
                    dx, dy, dz = spawn["x"] - x, spawn["y"] - y, spawn["z"] - z
                    if dx * dx + dy * dy + dz * dz <= limit:
                        found.append(spawn)
        found.sort(key=lambda s: s["guid"])
        return found


_GRIDS: Dict[Tuple[str, int], _MapGrid] = {}
_TEMPLATES: Dict[Tuple[str, int], Optional[dict]] = {}
_LOCK = threading.Lock()


def clear_cache() -> None:
    with _LOCK:
        _GRIDS.clear()
        _TEMPLATES.clear()


def _connect(path: str) -> sqlite3.Connection:
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    return con


def get_gameobject_template(path: str, entry: int) -> Optional[dict]:
    key = (path, int(entry))
    with _LOCK:
        if key in _TEMPLATES:
            return _TEMPLATES[key]
    con = _connect(path)
    try:
        row = con.execute(
            "SELECT * FROM gameobject_templates WHERE entry = ?", (int(entry),)
        ).fetchone()
    except sqlite3.OperationalError:
        row = None
    finally:
        con.close()
    template = dict(row) if row is not None else None
    with _LOCK:
        _TEMPLATES[key] = template
    return template


def load_map(path: str, map_id: int) -> _MapGrid:
    """Read every drawable spawn of ``map_id`` (blocking; run off the loop).

    Each spawn dict carries the template fields the create block needs, and
    the templates are cached for ``CMSG_GAMEOBJECT_QUERY`` at the same time.
    Spawns outside the default phase (``phase_mask & 1``), without a model,
    of an unsupported type, or that belong to a world event are dropped here
    once instead of on every scan.
    """
    key = (path, int(map_id))
    with _LOCK:
        grid = _GRIDS.get(key)
    if grid is not None:
        return grid
    con = _connect(path)
    try:
        try:
            rows = con.execute(
                "SELECT s.*, t.type, t.display_id, t.flags, t.faction, t.size,"
                " t.data0 FROM gameobject_spawns s"
                " JOIN gameobject_templates t ON t.entry = s.entry"
                " WHERE s.map = ? AND (s.phase_mask & 1) <> 0"
                " AND t.display_id <> 0"
                " AND NOT EXISTS (SELECT 1 FROM gameobject_events e"
                " WHERE e.guid = s.guid AND e.event > 0)",
                (int(map_id),),
            ).fetchall()
        except sqlite3.OperationalError:
            rows = []
    finally:
        con.close()
    spawns = []
    for row in rows:
        spawn = dict(row)
        if int(spawn["type"]) in UNSUPPORTED_TYPES:
            continue
        spawn["x"], spawn["y"], spawn["z"] = (
            float(spawn["x"]), float(spawn["y"]), float(spawn["z"]))
        spawns.append(spawn)
    grid = _MapGrid(spawns)
    with _LOCK:
        _GRIDS.setdefault(key, grid)
        return _GRIDS[key]


def is_map_loaded(path: str, map_id: int) -> bool:
    with _LOCK:
        return (path, int(map_id)) in _GRIDS


def gameobjects_near(path: str, map_id: int, x: float, y: float, z: float,
                     radius: float = GAMEOBJECT_VISIBILITY_RADIUS) -> List[dict]:
    """Spawns within ``radius`` yards; loads the map on first use."""
    if radius <= 0:
        return []
    return load_map(path, map_id).near(float(x), float(y), float(z), float(radius))


# ---------------------------------------------------------------------------
# Rotation
# ---------------------------------------------------------------------------
def spawn_rotation(spawn: Mapping) -> Tuple[float, float, float, float]:
    """World rotation quaternion ``(x, y, z, w)`` of a spawn.

    Most CMaNGOS rows store the quaternion; rows that leave it at zero only
    have an orientation, which is a rotation about the Z axis
    (TrinityCore ``GameObject::SetWorldRotationAngles``).
    """
    quat = tuple(float(spawn[f"rotation{i}"] or 0.0) for i in range(4))
    if not any(quat):
        half = float(spawn["o"] or 0.0) / 2.0
        return (0.0, 0.0, math.sin(half), math.cos(half))
    norm = math.sqrt(sum(c * c for c in quat))
    return tuple(c / norm for c in quat)  # type: ignore[return-value]


def packed_rotation(quat: Tuple[float, float, float, float]) -> int:
    """Pack a quaternion into the int64 behind ``UPDATEFLAG_ROTATION``.

    TrinityCore ``GameObject::UpdatePackedRotation``: x gets 22 bits, y and z
    21 bits each, w is implied (its sign is folded into the others).
    """
    pack_yz = 1 << 20
    pack_x = pack_yz << 1
    yz_mask = (pack_yz << 1) - 1
    x_mask = (pack_x << 1) - 1
    qx, qy, qz, qw = quat
    w_sign = 1 if qw >= 0 else -1
    x = (int(qx * pack_x) * w_sign) & x_mask
    y = (int(qy * pack_yz) * w_sign) & yz_mask
    z = (int(qz * pack_yz) * w_sign) & yz_mask
    packed = z | (y << 21) | (x << 42)
    # Reinterpret as the signed int64 the core writes.
    return packed - (1 << 64) if packed >= (1 << 63) else packed


def initial_state(spawn: Mapping) -> int:
    """``GO_STATE_*`` a spawn starts in.

    ``gameobject_addon.state`` wins when set; otherwise doors and buttons use
    their template's ``startOpen`` (``data0``) and everything else is READY.
    """
    state = int(spawn["state"]) if spawn["state"] is not None else -1
    if state >= 0:
        return state
    if int(spawn["type"]) in (GAMEOBJECT_TYPE_DOOR, GAMEOBJECT_TYPE_BUTTON) \
            and int(spawn["data0"] or 0):
        return GO_STATE_ACTIVE
    return GO_STATE_READY


# ---------------------------------------------------------------------------
# Packets
# ---------------------------------------------------------------------------
def _float_bits(value: float) -> int:
    return struct.unpack("<I", struct.pack("<f", float(value)))[0]


def _pack_guid(guid: int) -> bytes:
    mask = 0
    out = bytearray()
    for i in range(8):
        byte = (guid >> (i * 8)) & 0xFF
        if byte:
            mask |= 1 << i
            out.append(byte)
    return bytes([mask]) + bytes(out)


def build_gameobject_values(spawn: Mapping) -> Dict[int, int]:
    """``{field_index: uint32}`` for a spawn joined with its template."""
    entry = int(spawn["entry"])
    guid = gameobject_guid(int(spawn["guid"]), entry)
    values: Dict[int, int] = {
        F.OBJECT_FIELD_GUID: guid & 0xFFFFFFFF,
        F.OBJECT_FIELD_GUID + 1: (guid >> 32) & 0xFFFFFFFF,
        F.OBJECT_FIELD_TYPE: F.TYPEMASK_OBJECT | F.TYPEMASK_GAMEOBJECT,
        F.OBJECT_FIELD_ENTRY: entry,
        F.OBJECT_FIELD_SCALE_X: _float_bits(float(spawn["size"] or 1.0)),
        F.GAMEOBJECT_DISPLAYID: int(spawn["display_id"]),
        F.GAMEOBJECT_FLAGS: int(spawn["flags"]) & 0xFFFFFFFF,
        # Parent rotation is the identity unless the object rides a transport.
        F.GAMEOBJECT_PARENTROTATION + 3: _float_bits(1.0),
        F.GAMEOBJECT_FACTION: int(spawn["faction"]),
        F.GAMEOBJECT_BYTES_1: (
            (initial_state(spawn) & 0xFF)
            | ((int(spawn["type"]) & 0xFF) << 8)
            | ((int(spawn["anim_progress"]) & 0xFF) << 24)
        ),
    }
    return {index: value for index, value in values.items() if value}


def _update_type(spawn: Mapping) -> int:
    # TrinityCore ``Object::BuildCreateUpdateBlockForPlayer``: traps, duel
    # flags and battleground flags are "created" (CREATE_OBJECT2) so the
    # client plays their spawn animation; everything static is merely shown.
    if int(spawn["type"]) in (GAMEOBJECT_TYPE_TRAP, GAMEOBJECT_TYPE_DUEL_ARBITER,
                              GAMEOBJECT_TYPE_FLAGSTAND, GAMEOBJECT_TYPE_FLAGDROP):
        return UPDATETYPE_CREATE_OBJECT2
    return UPDATETYPE_CREATE_OBJECT


def build_gameobject_create_block(spawn: Mapping) -> bytes:
    """One create block: stationary position, low guid and packed rotation."""
    low_guid = int(spawn["guid"])
    guid = gameobject_guid(low_guid, int(spawn["entry"]))
    values = build_gameobject_values(spawn)

    mask_blocks = (F.GAMEOBJECT_END + 31) // 32
    mask = bytearray(mask_blocks * 4)
    for index in values:
        mask[(index // 32) * 4 + (index % 32) // 8] |= 1 << (index % 8)

    block = bytearray()
    block.append(_update_type(spawn))
    block += _pack_guid(guid)
    block.append(TYPEID_GAMEOBJECT)
    block += struct.pack("<H", GAMEOBJECT_UPDATE_FLAGS)
    block += struct.pack("<ffff", float(spawn["x"]), float(spawn["y"]),
                         float(spawn["z"]), float(spawn["o"] or 0.0))
    block += struct.pack("<I", low_guid & GAMEOBJECT_GUID_COUNTER_MASK)
    block += struct.pack("<q", packed_rotation(spawn_rotation(spawn)))
    block.append(mask_blocks)
    block += mask
    for index in sorted(values):
        block += struct.pack("<I", values[index] & 0xFFFFFFFF)
    return bytes(block)


def build_gameobject_update(spawns: Iterable[Mapping]) -> Optional[Tuple[int, bytes]]:
    """``(opcode, payload)`` creating ``spawns``, or ``None`` if empty."""
    blocks = [build_gameobject_create_block(spawn) for spawn in spawns]
    if not blocks:
        return None
    body = struct.pack("<I", len(blocks)) + b"".join(blocks)
    if len(body) > 100:
        return (Opcodes.SMSG_COMPRESSED_UPDATE_OBJECT,
                struct.pack("<I", len(body)) + zlib.compress(body))
    return Opcodes.SMSG_UPDATE_OBJECT, body


def _cstring(value) -> bytes:
    return str(value or "").encode("utf-8") + b"\x00"


def build_gameobject_query_response(entry: int, template: Optional[Mapping]) -> bytes:
    """``SMSG_GAMEOBJECT_QUERY_RESPONSE`` (TrinityCore 3.3.5 layout).

    An unknown entry is answered with the entry and the high bit set, which
    the client treats as "no such object".
    """
    if template is None:
        return struct.pack("<I", (int(entry) | 0x80000000) & 0xFFFFFFFF)
    out = bytearray()
    out += struct.pack("<III", int(entry), int(template["type"]),
                       int(template["display_id"]))
    out += _cstring(template["name"])
    out += b"\x00\x00\x00"                      # name2..name4
    out += _cstring(template["icon_name"])
    out += _cstring(template["cast_bar_caption"])
    out += _cstring(template["unk1"])
    for i in range(MAX_GAMEOBJECT_DATA):
        out += struct.pack("<I", int(template[f"data{i}"] or 0) & 0xFFFFFFFF)
    out += struct.pack("<f", float(template["size"] or 1.0))
    for i in range(1, MAX_GAMEOBJECT_QUEST_ITEMS + 1):
        out += struct.pack("<I", int(template[f"quest_item{i}"] or 0))
    return bytes(out)
