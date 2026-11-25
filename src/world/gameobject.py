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
import random
import sqlite3
import struct
import threading
import time
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
GAMEOBJECT_TYPE_CHAIR = 7
GAMEOBJECT_TYPE_TEXT = 9
GAMEOBJECT_TYPE_GOOBER = 10
GAMEOBJECT_TYPE_CAMERA = 13
GAMEOBJECT_TYPE_MAILBOX = 19
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

# ``GAMEOBJECT_DYNAMIC`` low word.  A quest chest/goober is only clickable for
# a player who needs it (ACTIVATE) and glitters for them (SPARKLE).
GO_DYNFLAG_LO_ACTIVATE = 0x01
GO_DYNFLAG_LO_SPARKLE = 0x08

# Distance at which an object can be used (TrinityCore INTERACTION_DISTANCE is
# 5 yards; the client's position lags a little behind the click).
GAMEOBJECT_USE_DISTANCE = 8.0
# Floor for respawn delays: a 0 in the dump still needs a timer.
MIN_RESPAWN_DELAY = 5

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

-- Same layout as ``creature_loot_template``: chest, herb and ore loot.
CREATE TABLE IF NOT EXISTS gameobject_loot_template(
  entry INTEGER NOT NULL,
  item INTEGER NOT NULL DEFAULT 0,
  chance_or_quest_chance REAL NOT NULL DEFAULT 0.0,
  group_id INTEGER NOT NULL DEFAULT 0,
  mincount_or_ref INTEGER NOT NULL DEFAULT 1,
  maxcount INTEGER NOT NULL DEFAULT 1,
  condition_id INTEGER NOT NULL DEFAULT 0,
  comments TEXT NOT NULL DEFAULT '',
  PRIMARY KEY(entry, item, group_id)
);

-- CMaNGOS pools: of the spawns in a pool only ``max_limit`` exist at a time
-- (rare chests, herbs and ore that share spawn points).  Pools can nest:
-- ``pool_pools`` makes a pool a member of a mother pool.
CREATE TABLE IF NOT EXISTS gameobject_pool_members(
  guid INTEGER PRIMARY KEY,
  pool INTEGER NOT NULL,
  chance REAL NOT NULL DEFAULT 0
);
-- ``pool_creature`` / ``pool_creature_template``: rare spawns and mobs that
-- share spawn points.
CREATE TABLE IF NOT EXISTS creature_pool_members(
  guid INTEGER PRIMARY KEY,
  pool INTEGER NOT NULL,
  chance REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS pool_templates(
  entry INTEGER PRIMARY KEY,
  max_limit INTEGER NOT NULL DEFAULT 0,
  description TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS pool_pools(
  pool_id INTEGER PRIMARY KEY,
  mother_pool INTEGER NOT NULL,
  chance REAL NOT NULL DEFAULT 0
);

-- Book and plaque pages (also readable items): one row per page, chained
-- through ``next_page``.
CREATE TABLE IF NOT EXISTS page_text(
  entry INTEGER PRIMARY KEY,
  text TEXT NOT NULL DEFAULT '',
  next_page INTEGER NOT NULL DEFAULT 0
);

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
    """All visible spawns of one map, bucketed into 64-yard cells.

    Rows stay in their ``sqlite3.Row`` form until a scan selects them.  A map
    can hold tens of thousands of spawns and converting every row to a dict
    when the map is first loaded cost over a second of GIL time, which the
    watchdog saw as login/combat stalls.  ``near`` only converts the handful
    of rows that are actually within range.
    """

    __slots__ = ("cells", "count")

    def __init__(self, spawns: Iterable[Mapping]):
        self.cells: Dict[Tuple[int, int], List[Mapping]] = {}
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
                    dx = float(spawn["x"]) - x
                    dy = float(spawn["y"]) - y
                    dz = float(spawn["z"]) - z
                    if dx * dx + dy * dy + dz * dz <= limit:
                        selected = dict(spawn)
                        selected["x"] = float(selected["x"])
                        selected["y"] = float(selected["y"])
                        selected["z"] = float(selected["z"])
                        found.append(selected)
        found.sort(key=lambda s: s["guid"])
        return found


_GRIDS: Dict[Tuple[str, int], _MapGrid] = {}
_TEMPLATES: Dict[Tuple[str, int], Optional[dict]] = {}
_LOCK = threading.Lock()


def clear_cache() -> None:
    with _LOCK:
        _GRIDS.clear()
        _TEMPLATES.clear()
        _QUEST_LOOT.clear()


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
    of an unsupported type, that belong to a world event, or with a negative
    respawn time (CMaNGOS: "only spawned by a script") are dropped here once
    instead of on every scan.
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
                "SELECT t.*, s.* FROM gameobject_spawns s"
                " JOIN gameobject_templates t ON t.entry = s.entry"
                " WHERE s.map = ? AND (s.phase_mask & 1) <> 0"
                " AND t.display_id <> 0 AND s.spawn_time_min >= 0"
                " AND NOT EXISTS (SELECT 1 FROM gameobject_events e"
                " WHERE e.guid = s.guid AND e.event > 0)",
                (int(map_id),),
            ).fetchall()
        except sqlite3.OperationalError:
            rows = []
    finally:
        con.close()
    # Keep the rows lazy: converting every spawn dict here was the dominant
    # cost of the first map load.  ``_MapGrid.near`` converts only the rows a
    # visibility scan actually returns.
    spawns = [row for row in rows
              if int(row["type"]) not in UNSUPPORTED_TYPES]
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


def build_gameobject_values(spawn: Mapping, state: Optional[int] = None,
                            dynamic: int = 0) -> Dict[int, int]:
    """``{field_index: uint32}`` for a spawn joined with its template.

    ``state`` overrides the spawn's start state (an opened door); ``dynamic``
    is the per-player ``GAMEOBJECT_DYNAMIC`` value (quest sparkle).
    """
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
        F.GAMEOBJECT_DYNAMIC: int(dynamic) & 0xFFFFFFFF,
        F.GAMEOBJECT_FACTION: int(spawn["faction"]),
        F.GAMEOBJECT_BYTES_1: (
            ((initial_state(spawn) if state is None else int(state)) & 0xFF)
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


def build_gameobject_create_block(spawn: Mapping, state: Optional[int] = None,
                                  dynamic: int = 0) -> bytes:
    """One create block: stationary position, low guid and packed rotation."""
    low_guid = int(spawn["guid"])
    guid = gameobject_guid(low_guid, int(spawn["entry"]))
    values = build_gameobject_values(spawn, state, dynamic)

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


def build_gameobject_update(spawns: Iterable[Mapping], state_of=None,
                            dynamic_of=None) -> Optional[Tuple[int, bytes]]:
    """``(opcode, payload)`` creating ``spawns``, or ``None`` if empty.

    ``state_of`` / ``dynamic_of`` map a spawn to its current state and
    per-player dynamic flags.
    """
    blocks = [
        build_gameobject_create_block(
            spawn,
            state_of(spawn) if state_of else None,
            dynamic_of(spawn) if dynamic_of else 0,
        )
        for spawn in spawns
    ]
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


# ---------------------------------------------------------------------------
# Type-specific template data (CMaNGOS ``GameObjectInfo`` unions)
# ---------------------------------------------------------------------------
def _data(template: Mapping, index: int) -> int:
    return int(template[f"data{index}"] or 0)


def lock_id(template: Mapping) -> int:
    """``Lock.dbc`` id; a locked object is opened by a spell, not by a click."""
    go_type = int(template["type"])
    if go_type in (GAMEOBJECT_TYPE_DOOR, GAMEOBJECT_TYPE_BUTTON):
        return _data(template, 1)
    if go_type in (GAMEOBJECT_TYPE_CHEST, GAMEOBJECT_TYPE_GOOBER,
                   GAMEOBJECT_TYPE_QUESTGIVER):
        return _data(template, 0)
    return 0


def loot_id(template: Mapping) -> int:
    """``gameobject_loot_template.entry`` of a chest (``chest.lootId``)."""
    if int(template["type"]) == GAMEOBJECT_TYPE_CHEST:
        return _data(template, 1)
    return 0


def chest_quest_id(template: Mapping) -> int:
    """``chest.questId``: the chest only opens while that quest is active."""
    if int(template["type"]) == GAMEOBJECT_TYPE_CHEST:
        return _data(template, 8)
    return 0


def is_consumable(template: Mapping) -> bool:
    """``chest.consumable``: the object despawns once it has been looted."""
    return int(template["type"]) == GAMEOBJECT_TYPE_CHEST and bool(_data(template, 3))


def restock_seconds(template: Mapping) -> int:
    """``chest.chestRestockTime``: a reusable chest refills after this long."""
    if int(template["type"]) == GAMEOBJECT_TYPE_CHEST:
        return _data(template, 2)
    return 0


def auto_close_seconds(template: Mapping) -> float:
    """``door.autoCloseTime`` / ``button.autoCloseTime`` (milliseconds)."""
    if int(template["type"]) in (GAMEOBJECT_TYPE_DOOR, GAMEOBJECT_TYPE_BUTTON):
        return _data(template, 2) / 1000.0
    return 0.0


# ``UNIT_STAND_STATE_SIT_LOW_CHAIR``; medium and high chairs follow (+1, +2).
UNIT_STAND_STATE_SIT_LOW_CHAIR = 4


def chair_stand_state(template: Mapping) -> int:
    """Stand state for ``chair.height`` (0 low, 1 medium, 2 high)."""
    return UNIT_STAND_STATE_SIT_LOW_CHAIR + max(0, min(2, _data(template, 1)))


def chair_seat(spawn: Mapping, x: float, y: float) -> Tuple[float, float]:
    """The free seat of a bench closest to ``(x, y)`` (``GameObject::Use`` CHAIR).

    The ``chair.slots`` seats sit on a line through the object's centre,
    perpendicular to its facing, ``size`` yards apart.
    """
    slots = max(1, _data(spawn, 0))
    size = float(spawn["size"] or 1.0)
    across = float(spawn["o"] or 0.0) + math.pi / 2.0
    best = None
    for slot in range(slots):
        offset = size * slot - size * (slots - 1) / 2.0
        sx = float(spawn["x"]) + offset * math.cos(across)
        sy = float(spawn["y"]) + offset * math.sin(across)
        dist = (sx - x) ** 2 + (sy - y) ** 2
        if best is None or dist < best[0]:
            best = (dist, sx, sy)
    return best[1], best[2]


def text_page(template: Mapping) -> int:
    """First ``page_text`` entry of a book/plaque (``text.pageID``)."""
    return _data(template, 0) if int(template["type"]) == GAMEOBJECT_TYPE_TEXT else 0


def camera_cinematic(template: Mapping) -> int:
    """``camera.cinematicId`` of a camera object (``CinematicSequences.dbc``)."""
    return _data(template, 1) if int(template["type"]) == GAMEOBJECT_TYPE_CAMERA else 0


MAX_PAGE_CHAIN = 64
PAGE_MISSING_TEXT = "Item page missing."


def build_page_text_responses(path: str, page_id: int) -> List[bytes]:
    """One ``SMSG_PAGE_TEXT_QUERY_RESPONSE`` per page of the chain.

    ``WorldSession::HandlePageTextQueryOpcode``: ``uint32 pageID, string
    text, uint32 nextPage``; a missing page ends the chain with a fixed text.
    """
    out = []
    con = sqlite3.connect(path)
    try:
        seen = set()
        while page_id and page_id not in seen and len(out) < MAX_PAGE_CHAIN:
            seen.add(page_id)
            try:
                row = con.execute("SELECT text, next_page FROM page_text WHERE entry = ?",
                                  (int(page_id),)).fetchone()
            except sqlite3.OperationalError:
                row = None
            text, next_page = (row[0], int(row[1] or 0)) if row else (PAGE_MISSING_TEXT, 0)
            out.append(struct.pack("<I", int(page_id)) + _cstring(text)
                       + struct.pack("<I", next_page))
            page_id = next_page
    finally:
        con.close()
    return out


def respawn_delay(spawn: Mapping) -> int:
    """Seconds until a despawned object returns (``spawntimesecsmin/max``)."""
    low = max(0, int(spawn["spawn_time_min"] or 0))
    high = max(low, int(spawn["spawn_time_max"] or 0))
    return max(MIN_RESPAWN_DELAY, random.randint(low, high) if high > low else low)


_QUEST_LOOT: Dict[Tuple[str, int], frozenset] = {}


def quest_loot_items(path: str, loot: int) -> frozenset:
    """Items a loot entry only drops for players on a quest (negative chance).

    References (negative ``mincount_or_ref``) are followed, like
    ``LootTemplate::HasQuestDropForPlayer``.
    """
    key = (path, int(loot))
    with _LOCK:
        if key in _QUEST_LOOT:
            return _QUEST_LOOT[key]
    items = set()
    con = sqlite3.connect(path)
    try:
        pending = [("gameobject_loot_template", int(loot))]
        seen = set()
        while pending and len(seen) < 64:
            table, entry = pending.pop()
            if (table, entry) in seen:
                continue
            seen.add((table, entry))
            try:
                rows = con.execute(
                    f"SELECT item, chance_or_quest_chance, mincount_or_ref FROM {table}"
                    " WHERE entry = ?", (entry,)).fetchall()
            except sqlite3.OperationalError:
                rows = []
            for item, chance, mincount_or_ref in rows:
                if int(mincount_or_ref or 0) < 0:
                    pending.append(("reference_loot_template", -int(mincount_or_ref)))
                elif float(chance or 0.0) < 0.0:
                    items.add(int(item))
    finally:
        con.close()
    result = frozenset(items)
    with _LOCK:
        _QUEST_LOOT[key] = result
    return result


def object_quest_id(template: Mapping) -> int:
    """``goober.questId`` / ``_generic.questID``: the quest the object is for."""
    go_type = int(template["type"])
    if go_type == GAMEOBJECT_TYPE_GOOBER:
        return _data(template, 1)
    if go_type == GAMEOBJECT_TYPE_GENERIC:
        return _data(template, 5)
    return 0


def goober_page(template: Mapping) -> int:
    return _data(template, 7) if int(template["type"]) == GAMEOBJECT_TYPE_GOOBER else 0


def goober_custom_anim(template: Mapping) -> int:
    return _data(template, 4) if int(template["type"]) == GAMEOBJECT_TYPE_GOOBER else 0


def goober_spell(template: Mapping) -> int:
    return _data(template, 10) if int(template["type"]) == GAMEOBJECT_TYPE_GOOBER else 0


def goober_consumable(template: Mapping) -> bool:
    return int(template["type"]) == GAMEOBJECT_TYPE_GOOBER and bool(_data(template, 5))


def goober_reset_seconds(template: Mapping) -> float:
    """``goober.autoClose`` (ms); TrinityCore resets at once when it is 0."""
    return max(1.0, _data(template, 3) / 1000.0)


def is_quest_object(template: Mapping, path: str) -> bool:
    """True for objects whose use depends on the player's quests."""
    go_type = int(template["type"])
    if go_type == GAMEOBJECT_TYPE_QUESTGIVER:
        from src.world import quests
        return quests.is_quest_giver(path, int(template["entry"]), quests.GIVER_GAMEOBJECT)
    if go_type in (GAMEOBJECT_TYPE_GOOBER, GAMEOBJECT_TYPE_GENERIC):
        return bool(object_quest_id(template))
    if go_type != GAMEOBJECT_TYPE_CHEST:
        return False
    return bool(chest_quest_id(template) or quest_loot_items(path, loot_id(template)))


# ---------------------------------------------------------------------------
# Pools
# ---------------------------------------------------------------------------
def _roll_member(candidates: List[Tuple[int, float]]) -> Optional[int]:
    """One member, CMaNGOS ``PoolGroup::RollOne``: explicit chances first,
    then an equal-chance pick among the members without one."""
    if not candidates:
        return None
    explicit = [(m, c) for m, c in candidates if c > 0]
    if explicit:
        roll = random.random() * 100.0
        for member, chance in explicit:
            roll -= chance
            if roll < 0:
                return member
    equal = [m for m, c in candidates if c <= 0]
    return random.choice(equal or [m for m, _ in candidates])


class SpawnPools:
    """Which pooled spawns currently exist (CMaNGOS ``PoolManager``).

    One instance per spawn kind: game objects and creatures have separate
    guid ranges and member tables but share ``pool_templates``/``pool_pools``.
    """

    def __init__(self, members=(), limits=None, children=()):
        self.member_pool: Dict[int, int] = {}
        self.members: Dict[int, List[Tuple[int, float]]] = {}
        for guid, pool, chance in members:
            self.member_pool[int(guid)] = int(pool)
            self.members.setdefault(int(pool), []).append((int(guid), float(chance or 0)))
        self.limits: Dict[int, int] = {int(k): int(v) for k, v in (limits or {}).items()}
        self.children: Dict[int, List[Tuple[int, float]]] = {}
        self.parent: Dict[int, int] = {}
        for child, mother, chance in children:
            self.children.setdefault(int(mother), []).append((int(child), float(chance or 0)))
            self.parent[int(child)] = int(mother)
        self.active: set = set()
        self.pending: List[Tuple[float, int]] = []      # (when, pool) re-rolls
        for pool in sorted(set(self.members) | set(self.children)):
            if pool not in self.parent:
                self._spawn_pool(pool, self.limits.get(pool, 0))

    @classmethod
    def load(cls, path: str, member_table: str = "gameobject_pool_members") -> "SpawnPools":
        if member_table not in ("gameobject_pool_members", "creature_pool_members"):
            raise ValueError(f"unknown pool member table {member_table}")
        con = sqlite3.connect(path)
        try:
            try:
                members = con.execute(
                    f"SELECT guid, pool, chance FROM {member_table}").fetchall()
                limits = dict(con.execute("SELECT entry, max_limit FROM pool_templates"))
                children = con.execute(
                    "SELECT pool_id, mother_pool, chance FROM pool_pools").fetchall()
            except sqlite3.OperationalError:
                members, limits, children = [], {}, []
        finally:
            con.close()
        # Only keep the pool tree above pools that hold members of this kind.
        used = {pool for _guid, pool, _chance in members}
        while True:
            grown = used | {mother for child, mother, _c in children if child in used}
            if grown == used:
                break
            used = grown
        children = [row for row in children if row[0] in used]
        return cls(members, limits, children)

    def _spawn_pool(self, pool: int, count: int) -> None:
        """Activate ``count`` members (0 = all) of ``pool`` or of its child pools."""
        if pool in self.children:
            options = list(self.children[pool])
            if count <= 0:
                # "Spawn every member" does not need to remove picks one at a
                # time; the old O(n^2) list rebuilds made pool loading dominate
                # the first login for large world-event pools.
                for child, _chance in options:
                    self._spawn_pool(child, self.limits.get(child, 0))
                return
            picks = min(count, len(options))
            for _ in range(picks):
                child = _roll_member(options)
                options = [o for o in options if o[0] != child]
                self._spawn_pool(child, self.limits.get(child, 0))
            return
        options = [m for m in self.members.get(pool, []) if m[0] not in self.active]
        if count <= 0:
            self.active.update(guid for guid, _chance in options)
            return
        picks = min(count, len(options))
        for _ in range(picks):
            guid = _roll_member(options)
            options = [o for o in options if o[0] != guid]
            self.active.add(guid)

    def is_hidden(self, guid: int) -> bool:
        return int(guid) in self.member_pool and int(guid) not in self.active

    def on_despawn(self, guid: int, respawn_at: float) -> None:
        """A pooled object was used up; its pool picks a member again later."""
        guid = int(guid)
        if guid not in self.member_pool:
            return
        self.active.discard(guid)
        self.pending.append((respawn_at, self.member_pool[guid]))

    def due(self, now: float) -> List[int]:
        """Re-roll pools whose respawn time came; returns the new members."""
        ready = [p for p in self.pending if p[0] <= now]
        if not ready:
            return []
        self.pending = [p for p in self.pending if p[0] > now]
        spawned = []
        for _when, pool in ready:
            options = [m for m in self.members.get(pool, []) if m[0] not in self.active]
            guid = _roll_member(options)
            if guid is not None:
                self.active.add(guid)
                spawned.append(guid)
        return spawned


GameObjectPools = SpawnPools


def load_creature_pools(path: str) -> SpawnPools:
    return SpawnPools.load(path, "creature_pool_members")


# ---------------------------------------------------------------------------
# Runtime state shared by every session
# ---------------------------------------------------------------------------
class GameObjectRuntime:
    """What changed since the spawns were loaded: despawns, states, loot.

    One instance lives on the world server; all times are ``time.monotonic``.
    """

    def __init__(self) -> None:
        self.despawned: Dict[int, float] = {}        # guid -> respawn time
        self.states: Dict[int, Tuple[int, float]] = {}  # guid -> (state, reset at; 0 = never)
        self.loot: Dict[int, dict] = {}             # guid -> {"items": [...], "money": int}
        self.restock: Dict[int, float] = {}         # guid -> loot regenerates at
        self.looter: Dict[int, int] = {}            # guid -> player guid
        self.pools: Optional[GameObjectPools] = None  # loaded on first use

    # -- spawn ---------------------------------------------------------
    def is_spawned(self, guid: int, now: Optional[float] = None) -> bool:
        if self.pools is not None and self.pools.is_hidden(guid):
            return False
        respawn_at = self.despawned.get(int(guid))
        if respawn_at is None:
            return True
        if (time.monotonic() if now is None else now) >= respawn_at:
            del self.despawned[int(guid)]
            return True
        return False

    def despawn(self, guid: int, delay: float, now: Optional[float] = None) -> None:
        guid = int(guid)
        respawn_at = (time.monotonic() if now is None else now) + float(delay)
        self.despawned[guid] = respawn_at
        if self.pools is not None:
            self.pools.on_despawn(guid, respawn_at)
        self.states.pop(guid, None)
        self.loot.pop(guid, None)
        self.restock.pop(guid, None)
        self.looter.pop(guid, None)

    # -- state ---------------------------------------------------------
    def state(self, guid: int) -> Optional[int]:
        entry = self.states.get(int(guid))
        return entry[0] if entry is not None else None

    def set_state(self, guid: int, state: int, reset_after: float = 0.0,
                  now: Optional[float] = None) -> None:
        now = time.monotonic() if now is None else now
        self.states[int(guid)] = (int(state), now + reset_after if reset_after > 0 else 0.0)

    def expired_states(self, now: Optional[float] = None) -> list:
        """Guids whose temporary state (an opened door) has run out."""
        now = time.monotonic() if now is None else now
        expired = [guid for guid, (_state, reset_at) in self.states.items()
                   if reset_at and now >= reset_at]
        for guid in expired:
            del self.states[guid]
        return expired

    # -- loot ----------------------------------------------------------
    def stored_loot(self, guid: int, now: Optional[float] = None) -> Optional[dict]:
        guid = int(guid)
        restock_at = self.restock.get(guid)
        if restock_at is not None and (time.monotonic() if now is None else now) >= restock_at:
            del self.restock[guid]
            self.loot.pop(guid, None)
        return self.loot.get(guid)

    def respawn_pools(self, now: Optional[float] = None) -> List[int]:
        """Activate the pool members whose turn came; they are spawned now."""
        if self.pools is None:
            return []
        spawned = self.pools.due(time.monotonic() if now is None else now)
        for guid in spawned:
            self.despawned.pop(guid, None)
        return spawned

    def clear(self) -> None:
        self.pools = None
        self.despawned.clear()
        self.states.clear()
        self.loot.clear()
        self.restock.clear()
        self.looter.clear()
