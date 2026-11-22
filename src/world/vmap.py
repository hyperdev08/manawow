"""Static world-model collision data from extracted 3.3.5a VMaps.

The extractor writes three kinds of file:

``<map>.vmtree``
    The global map BIH.  It does not contain placements for tiled maps.

``<map>_<tileY>_<tileX>.vmtile``
    Placements (position, rotation, scale and bounds) for every WMO/M2 model
    touching that tile.

``<model>.vmo``
    The model's collision geometry.  For WMOs we use the per-group bounds,
    which are small enough to approximate walls and floors accurately without
    intersecting millions of triangles.  M2 placements use the transformed
    model bound.

The server treats these as *static* blockers.  Dynamic game objects (doors,
bridges that open, ...) are not part of the client VMap set and remain the
responsibility of spell/aura code.
"""
from __future__ import annotations

import math
import os
import struct
from dataclasses import dataclass
from typing import Optional

from .client_data import ClientData, VMAP_COORD_OFFSET

VMAP_MAGIC = b"VMAP_4.1"
MAX_RAY_DISTANCE = 1.0e9


def _segment_intersects_aabb(start, end, low, high, epsilon: float = 1e-6) -> bool:
    """Slab test with a special case for two endpoints in the same volume.

    A ray whose *both* ends are inside one room-sized WMO group is allowed
    (otherwise every interior fight would fail).  A ray with exactly one end
    inside, however, has to cross the group boundary and is treated as blocked
    by that wall.  This is the conservative choice: without triangle data we
    cannot tell a doorway from a solid wall, and blocking is safer than
    letting spells pass through a building.
    """
    inside_start = all(low[i] - epsilon <= start[i] <= high[i] + epsilon
                       for i in range(3))
    inside_end = all(low[i] - epsilon <= end[i] <= high[i] + epsilon
                     for i in range(3))
    if inside_start and inside_end:
        return False
    t_min, t_max = 0.0, 1.0
    for axis in range(3):
        origin = start[axis]
        direction = end[axis] - origin
        if abs(direction) < epsilon:
            if origin < low[axis] or origin > high[axis]:
                return False
            continue
        inv = 1.0 / direction
        t1 = (low[axis] - origin) * inv
        t2 = (high[axis] - origin) * inv
        if t1 > t2:
            t1, t2 = t2, t1
        t_min = max(t_min, t1)
        t_max = min(t_max, t2)
        if t_min > t_max:
            return False
    return t_max >= 0.0 and t_min <= 1.0


@dataclass(frozen=True)
class ModelBox:
    low: tuple[float, float, float]
    high: tuple[float, float, float]


class _BVHNode:
    __slots__ = ("low", "high", "left", "right", "triangles")

    def __init__(self, low, high):
        self.low = low
        self.high = high
        self.left: Optional[_BVHNode] = None
        self.right: Optional[_BVHNode] = None
        self.triangles: Optional[list[int]] = None


def _triangle_bounds(vertices, triangle):
    a, b, c = (vertices[triangle[0]], vertices[triangle[1]],
               vertices[triangle[2]])
    return ((min(a[0], b[0], c[0]), min(a[1], b[1], c[1]),
             min(a[2], b[2], c[2])),
            (max(a[0], b[0], c[0]), max(a[1], b[1], c[1]),
             max(a[2], b[2], c[2])))


def _bvh_build(vertices, triangles, indices, depth=0,
               _bounds=None, _centroids=None) -> _BVHNode:
    if _bounds is None:
        _bounds = [_triangle_bounds(vertices, triangle)
                   for triangle in triangles]
        _centroids = [((low[0] + high[0]) * 0.5,
                       (low[1] + high[1]) * 0.5,
                       (low[2] + high[2]) * 0.5)
                      for low, high in _bounds]
    low = [1.0e30] * 3
    high = [-1.0e30] * 3
    for index in indices:
        tri_low, tri_high = _bounds[index]
        low[0] = min(low[0], tri_low[0])
        low[1] = min(low[1], tri_low[1])
        low[2] = min(low[2], tri_low[2])
        high[0] = max(high[0], tri_high[0])
        high[1] = max(high[1], tri_high[1])
        high[2] = max(high[2], tri_high[2])
    node = _BVHNode(tuple(low), tuple(high))
    if len(indices) <= 12 or depth >= 28:
        node.triangles = list(indices)
        return node
    axis = max(range(3), key=lambda value: high[value] - low[value])
    order = sorted(range(len(indices)),
                   key=lambda position: _centroids[indices[position]][axis])
    midpoint = len(order) // 2
    node.left = _bvh_build(vertices, triangles,
                           [indices[position] for position in order[:midpoint]],
                           depth + 1, _bounds, _centroids)
    node.right = _bvh_build(vertices, triangles,
                            [indices[position] for position in order[midpoint:]],
                            depth + 1, _bounds, _centroids)
    return node


def _ray_aabb(low, high, origin, delta) -> bool:
    t_min, t_max = 0.0, 1.0
    for axis in range(3):
        if abs(delta[axis]) < 1e-12:
            if origin[axis] < low[axis] or origin[axis] > high[axis]:
                return False
            continue
        inv = 1.0 / delta[axis]
        first = (low[axis] - origin[axis]) * inv
        second = (high[axis] - origin[axis]) * inv
        if first > second:
            first, second = second, first
        t_min = max(t_min, first)
        t_max = min(t_max, second)
        if t_min > t_max:
            return False
    return t_max >= 0.0 and t_min <= 1.0


def _ray_triangle(origin, delta, a, b, c) -> bool:
    edge1 = (b[0] - a[0], b[1] - a[1], b[2] - a[2])
    edge2 = (c[0] - a[0], c[1] - a[1], c[2] - a[2])
    pvec = (delta[1] * edge2[2] - delta[2] * edge2[1],
            delta[2] * edge2[0] - delta[0] * edge2[2],
            delta[0] * edge2[1] - delta[1] * edge2[0])
    determinant = edge1[0] * pvec[0] + edge1[1] * pvec[1] + edge1[2] * pvec[2]
    if abs(determinant) < 1e-9:
        return False
    inv = 1.0 / determinant
    tvec = (origin[0] - a[0], origin[1] - a[1], origin[2] - a[2])
    u = (tvec[0] * pvec[0] + tvec[1] * pvec[1]
         + tvec[2] * pvec[2]) * inv
    if u < -1e-6 or u > 1.0 + 1e-6:
        return False
    qvec = (tvec[1] * edge1[2] - tvec[2] * edge1[1],
            tvec[2] * edge1[0] - tvec[0] * edge1[2],
            tvec[0] * edge1[1] - tvec[1] * edge1[0])
    v = (delta[0] * qvec[0] + delta[1] * qvec[1]
         + delta[2] * qvec[2]) * inv
    if v < -1e-6 or u + v > 1.0 + 1e-6:
        return False
    t = (edge2[0] * qvec[0] + edge2[1] * qvec[1]
         + edge2[2] * qvec[2]) * inv
    return -1e-6 <= t <= 1.0 + 1e-6


def _bvh_intersect(node: _BVHNode, vertices, triangles,
                   origin, delta) -> bool:
    if not _ray_aabb(node.low, node.high, origin, delta):
        return False
    if node.triangles is not None:
        for index in node.triangles:
            triangle = triangles[index]
            if _ray_triangle(origin, delta, vertices[triangle[0]],
                             vertices[triangle[1]], vertices[triangle[2]]):
                return True
        return False
    return ((node.left is not None
             and _bvh_intersect(node.left, vertices, triangles,
                                origin, delta))
            or (node.right is not None
                and _bvh_intersect(node.right, vertices, triangles,
                                   origin, delta)))


class VmoGroup:
    """One WMO collision group, in the model's local coordinate space."""

    __slots__ = ("low", "high", "vertices", "triangles", "_bvh")

    def __init__(self, low, high, vertices, triangles):
        self.low = low
        self.high = high
        self.vertices = vertices
        self.triangles = triangles
        self._bvh: Optional[_BVHNode] = None

    def ray_blocked(self, origin, delta) -> bool:
        if not self.triangles:
            return False
        if not _ray_aabb(self.low, self.high, origin, delta):
            return False
        if self._bvh is None:
            self._bvh = _bvh_build(self.vertices, self.triangles,
                                   list(range(len(self.triangles))))
        return _bvh_intersect(self._bvh, self.vertices, self.triangles,
                              origin, delta)


def read_vmo_groups(path: str) -> Optional[list[VmoGroup]]:
    """Read local-space vertices, triangles and bounds from a ``.vmo``."""
    try:
        with open(path, "rb") as handle:
            if _read_exact(handle, 8) != VMAP_MAGIC:
                return None
            if _read_exact(handle, 4) != b"WMOD":
                return None
            _read_exact(handle, 8)               # chunk size + root WMO id
            if _read_exact(handle, 4) != b"GMOD":
                return None
            group_count = struct.unpack("<I", _read_exact(handle, 4))[0]
            if group_count > 4096:
                return None
            groups = []
            for _ in range(group_count):
                low = struct.unpack("<3f", _read_exact(handle, 12))
                high = struct.unpack("<3f", _read_exact(handle, 12))
                _read_exact(handle, 8)           # flags + group WMO id
                if _read_exact(handle, 4) != b"VERT":
                    return groups or None
                _chunk_size, vertex_count = struct.unpack(
                    "<2I", _read_exact(handle, 8))
                if vertex_count > 1000000:
                    return groups or None
                raw = struct.unpack("<%df" % (vertex_count * 3),
                                    _read_exact(handle, vertex_count * 12))
                vertices = [tuple(float(value) for value in
                                  raw[index:index + 3])
                            for index in range(0, len(raw), 3)]
                if _read_exact(handle, 4) != b"TRIM":
                    return groups or None
                _chunk_size, triangle_count = struct.unpack(
                    "<2I", _read_exact(handle, 8))
                if triangle_count > 3000000:
                    return groups or None
                raw_triangles = struct.unpack(
                    "<%dI" % (triangle_count * 3),
                    _read_exact(handle, triangle_count * 12))
                triangles = [tuple(int(value) for value in
                                   raw_triangles[index:index + 3])
                             for index in range(0, len(raw_triangles), 3)]
                if _read_exact(handle, 4) != b"MBIH":
                    return groups or None
                _skip_bih(handle)
                if _read_exact(handle, 4) != b"LIQU":
                    return groups or None
                liquid_size = struct.unpack("<I", _read_exact(handle, 4))[0]
                if liquid_size:
                    handle.seek(liquid_size, os.SEEK_CUR)
                groups.append(VmoGroup(
                    tuple(float(v) for v in low),
                    tuple(float(v) for v in high),
                    vertices, triangles))
            return groups
    except (OSError, ValueError, struct.error):
        return None


@dataclass
class ModelSpawn:
    pos: tuple[float, float, float]
    rot: tuple[float, float, float]
    scale: float
    name: str
    flags: int
    low: Optional[tuple[float, float, float]] = None
    high: Optional[tuple[float, float, float]] = None
    # Populated by :class:`VMapMap` when the .vmo is available.
    parts: Optional[list[ModelBox]] = None
    model_loaded: bool = False

    @property
    def is_wmo(self) -> bool:
        return self.name.lower().endswith(".wmo")

    def world_bounds(self) -> Optional[tuple[tuple[float, float, float],
                                             tuple[float, float, float]]]:
        """Bounds converted from the extractor's reflected space."""
        if self.low is None or self.high is None:
            return None
        return ((VMAP_COORD_OFFSET - self.high[0],
                 VMAP_COORD_OFFSET - self.high[1], self.low[2]),
                (VMAP_COORD_OFFSET - self.low[0],
                 VMAP_COORD_OFFSET - self.low[1], self.high[2]))


def _read_exact(handle, size: int) -> bytes:
    data = handle.read(size)
    if len(data) != size:
        raise ValueError("truncated vmap chunk")
    return data


def _skip_bih(handle) -> None:
    _read_exact(handle, 24)                       # bounds low/high
    tree_size = struct.unpack("<I", _read_exact(handle, 4))[0]
    handle.seek(tree_size * 4, os.SEEK_CUR)
    count = struct.unpack("<I", _read_exact(handle, 4))[0]
    handle.seek(count * 4, os.SEEK_CUR)


@dataclass
class VMapTile:
    tile_x: int
    tile_y: int
    spawns: list[ModelSpawn]


def _read_spawn(data: bytes, cursor: int) -> tuple[Optional[ModelSpawn], int]:
    """Parse one ``ModelSpawn`` from ``data`` at ``cursor``."""
    try:
        flags = struct.unpack_from("<I", data, cursor)[0]
        cursor += 4 + 2 + 4
        pos = struct.unpack_from("<3f", data, cursor)
        cursor += 12
        rot = struct.unpack_from("<3f", data, cursor)
        cursor += 12
        scale = struct.unpack_from("<f", data, cursor)[0]
        cursor += 4
        low = high = None
        if flags & 4:
            low = struct.unpack_from("<3f", data, cursor)
            cursor += 12
            high = struct.unpack_from("<3f", data, cursor)
            cursor += 12
        name_len = struct.unpack_from("<I", data, cursor)[0]
        cursor += 4
        if name_len > 4096 or cursor + name_len > len(data):
            return None, cursor
        name = data[cursor:cursor + name_len].decode("latin-1",
                                                     errors="replace")
        cursor += name_len
        return ModelSpawn(
            tuple(float(v) for v in pos),
            tuple(float(v) for v in rot),
            float(scale or 1.0),
            name,
            int(flags),
            tuple(float(v) for v in low) if low is not None else None,
            tuple(float(v) for v in high) if high is not None else None,
        ), cursor
    except (struct.error, IndexError):
        return None, cursor


def load_vmap_tree_globals(path: str) -> list[ModelSpawn]:
    """Read global WMO placements from a non-tiled ``.vmtree``.

    Dungeons and other maps without ADT tiles keep their single world model in
    the map tree instead of a ``.vmtile``.  Tiled maps return an empty list.
    """
    try:
        with open(path, "rb") as handle:
            data = handle.read()
    except OSError:
        return []
    if len(data) < 14 or data[:8] != VMAP_MAGIC:
        return []
    if data[8]:
        return []                               # tiled map
    try:
        cursor = 9
        if data[cursor:cursor + 4] != b"NODE":
            return []
        cursor += 4
        # Skip the BIH: bounds (24), tree, objects.
        cursor += 24
        tree_size = struct.unpack_from("<I", data, cursor)[0]
        cursor += 4 + tree_size * 4
        count = struct.unpack_from("<I", data, cursor)[0]
        cursor += 4 + count * 4
        if data[cursor:cursor + 4] != b"GOBJ":
            return []
        cursor += 4
    except struct.error:
        return []
    spawns = []
    while cursor < len(data):
        spawn, next_cursor = _read_spawn(data, cursor)
        if spawn is None or next_cursor <= cursor:
            break
        spawns.append(spawn)
        cursor = next_cursor
        # Non-tiled trees normally contain exactly one WMO; stop at the
        # trailing data rather than treating arbitrary bytes as spawns.
        if len(spawns) >= 64:
            break
    return spawns


def load_vmap_tile(path: str, tile_x: int, tile_y: int
                   ) -> Optional[VMapTile]:
    try:
        with open(path, "rb") as handle:
            data = handle.read()
    except OSError:
        return None
    if len(data) < 12 or data[:8] != VMAP_MAGIC:
        return None
    count = struct.unpack_from("<I", data, 8)[0]
    if count > 200000:
        return None
    cursor = 12
    spawns: list[ModelSpawn] = []
    try:
        for _ in range(count):
            flags = struct.unpack_from("<I", data, cursor)[0]
            cursor += 4
            cursor += 2                              # adtId
            cursor += 4                              # unique id
            pos = struct.unpack_from("<3f", data, cursor)
            cursor += 12
            rot = struct.unpack_from("<3f", data, cursor)
            cursor += 12
            scale = struct.unpack_from("<f", data, cursor)[0]
            cursor += 4
            low = high = None
            # Bit 2 (value 4) is MOD_HAS_BOUND in the 3.3.5 extractor.
            if flags & 4:
                low = struct.unpack_from("<3f", data, cursor)
                cursor += 12
                high = struct.unpack_from("<3f", data, cursor)
                cursor += 12
            name_len = struct.unpack_from("<I", data, cursor)[0]
            cursor += 4
            if name_len > 4096 or cursor + name_len > len(data):
                return None
            name = data[cursor:cursor + name_len].decode("latin-1",
                                                         errors="replace")
            cursor += name_len
            cursor += 4                              # tree reference
            spawns.append(ModelSpawn(
                tuple(float(v) for v in pos),
                tuple(float(v) for v in rot),
                float(scale or 1.0),
                name,
                int(flags),
                tuple(float(v) for v in low) if low is not None else None,
                tuple(float(v) for v in high) if high is not None else None,
            ))
    except (struct.error, IndexError):
        return None
    return VMapTile(int(tile_x), int(tile_y), spawns)


def _rotation_matrix(rot: tuple[float, float, float]
                     ) -> tuple[tuple[float, ...], ...]:
    """``G3D::Matrix3::fromEulerAnglesZYX(yaw, pitch, roll)``."""
    yaw = math.radians(rot[1])
    pitch = math.radians(rot[0])
    roll = math.radians(rot[2])
    cy, sy = math.cos(yaw), math.sin(yaw)
    cp, sp = math.cos(pitch), math.sin(pitch)
    cr, sr = math.cos(roll), math.sin(roll)
    # Rz(yaw) * Ry(pitch) * Rx(roll)
    return (
        (cy * cp, cy * sp * sr - sy * cr, cy * sp * cr + sy * sr),
        (sy * cp, sy * sp * sr + cy * cr, sy * sp * cr - cy * sr),
        (-sp, cp * sr, cp * cr),
    )


def _transform_box(low, high, spawn: ModelSpawn) -> ModelBox:
    matrix = _rotation_matrix(spawn.rot)
    scale = float(spawn.scale or 1.0)
    corners = []
    for x in (low[0], high[0]):
        for y in (low[1], high[1]):
            for z in (low[2], high[2]):
                vx, vy, vz = x * scale, y * scale, z * scale
                corners.append((
                    matrix[0][0] * vx + matrix[0][1] * vy + matrix[0][2] * vz
                    + spawn.pos[0],
                    matrix[1][0] * vx + matrix[1][1] * vy + matrix[1][2] * vz
                    + spawn.pos[1],
                    matrix[2][0] * vx + matrix[2][1] * vy + matrix[2][2] * vz
                    + spawn.pos[2],
                ))
    return ModelBox(
        tuple(min(c[i] for c in corners) for i in range(3)),
        tuple(max(c[i] for c in corners) for i in range(3)),
    )


class VMapMap:
    """Lazy tile/model cache and LOS queries for one map."""

    def __init__(self, data: ClientData, map_id: int,
                 model_cache_limit: int = 48, tile_cache_limit: int = 64):
        self.data = data
        self.map_id = int(map_id)
        self.model_cache_limit = max(4, int(model_cache_limit))
        self.tile_cache_limit = max(4, int(tile_cache_limit))
        self._tiles: dict[tuple[int, int], Optional[VMapTile]] = {}
        self._tile_order: list[tuple[int, int]] = []
        self._models: dict[str, Optional[list[VmoGroup]]] = {}
        self._model_order: list[str] = []
        self._globals: Optional[list[ModelSpawn]] = None

    def _tile(self, tile_x: int, tile_y: int) -> Optional[VMapTile]:
        key = (int(tile_x), int(tile_y))
        if key in self._tiles:
            return self._tiles[key]
        tile = load_vmap_tile(
            self.data.vmap_tile_path(self.map_id, key[0], key[1]),
            key[0], key[1])
        self._tiles[key] = tile
        self._tile_order.append(key)
        while len(self._tile_order) > self.tile_cache_limit:
            old = self._tile_order.pop(0)
            if old != key:
                self._tiles.pop(old, None)
        return tile

    def _model_groups(self, spawn: ModelSpawn) -> Optional[list[VmoGroup]]:
        if not spawn.is_wmo:
            return None
        name = spawn.name
        if name in self._models:
            return self._models[name]
        groups = read_vmo_groups(self.data.vmap_model_path(name))
        self._models[name] = groups
        self._model_order.append(name)
        while len(self._model_order) > self.model_cache_limit:
            old = self._model_order.pop(0)
            if old != name:
                self._models.pop(old, None)
        return groups

    def _spawn_boxes(self, spawn: ModelSpawn) -> list[ModelBox]:
        if spawn.model_loaded:
            return spawn.parts or []
        spawn.model_loaded = True
        groups = self._model_groups(spawn)
        if groups:
            # The parts are in the extractor's reflected space; convert each
            # transformed group box back to world space for the fallback path.
            converted = []
            for group in groups:
                part = _transform_box(group.low, group.high, spawn)
                converted.append(ModelBox(
                    (VMAP_COORD_OFFSET - part.high[0],
                     VMAP_COORD_OFFSET - part.high[1], part.low[2]),
                    (VMAP_COORD_OFFSET - part.low[0],
                     VMAP_COORD_OFFSET - part.low[1], part.high[2]),
                ))
            spawn.parts = converted
            return converted
        bounds = spawn.world_bounds()
        spawn.parts = [ModelBox(*bounds)] if bounds is not None else []
        return spawn.parts

    @staticmethod
    def _inverse_rotation(rot):
        matrix = _rotation_matrix(rot)
        return tuple(tuple(matrix[row][column] for row in range(3))
                     for column in range(3))

    def _exact_blocks(self, spawn: ModelSpawn, start, end) -> bool:
        groups = self._model_groups(spawn)
        if not groups or not any(group.triangles for group in groups):
            return False
        inverse = self._inverse_rotation(spawn.rot)
        scale = float(spawn.scale or 1.0)
        if abs(scale) < 1e-6:
            scale = 1.0
        inv_scale = 1.0 / scale
        stored_start = (VMAP_COORD_OFFSET - start[0],
                        VMAP_COORD_OFFSET - start[1], start[2])
        stored_end = (VMAP_COORD_OFFSET - end[0],
                      VMAP_COORD_OFFSET - end[1], end[2])
        local_start = [0.0, 0.0, 0.0]
        local_delta = [0.0, 0.0, 0.0]
        for axis in range(3):
            local_start[axis] = (inverse[axis][0]
                                 * (stored_start[0] - spawn.pos[0])
                                 + inverse[axis][1]
                                 * (stored_start[1] - spawn.pos[1])
                                 + inverse[axis][2]
                                 * (stored_start[2] - spawn.pos[2])) * inv_scale
            local_delta[axis] = (inverse[axis][0]
                                 * (stored_end[0] - stored_start[0])
                                 + inverse[axis][1]
                                 * (stored_end[1] - stored_start[1])
                                 + inverse[axis][2]
                                 * (stored_end[2] - stored_start[2])) * inv_scale
        origin = tuple(local_start)
        delta = tuple(local_delta)
        for group in groups:
            if group.ray_blocked(origin, delta):
                return True
        return False

    def _global_spawns(self) -> list[ModelSpawn]:
        if self._globals is None:
            self._globals = load_vmap_tree_globals(
                self.data.vmap_tree_path(self.map_id))
        return self._globals

    def _spawn_blocks(self, spawn: ModelSpawn, start, end,
                      min_x: float, max_x: float,
                      min_y: float, max_y: float) -> bool:
        bounds = spawn.world_bounds()
        if bounds is None:
            return False
        low, high = bounds
        if (high[0] < min_x - 1.0 or low[0] > max_x + 1.0
                or high[1] < min_y - 1.0 or low[1] > max_y + 1.0):
            return False
        # Exact WMO geometry wins when it exists; otherwise fall back to the
        # transformed group/model bounds (and always for M2 placements).
        if spawn.is_wmo:
            groups = self._model_groups(spawn)
            if groups and any(group.triangles for group in groups):
                return self._exact_blocks(spawn, start, end)
        for box in self._spawn_boxes(spawn):
            if _segment_intersects_aabb(start, end, box.low, box.high):
                return True
        return False

    def segment_clear(self, start: tuple[float, float, float],
                      end: tuple[float, float, float]) -> bool:
        min_x, max_x = sorted((start[0], end[0]))
        min_y, max_y = sorted((start[1], end[1]))
        # One tile of margin catches models whose bound sticks over a border.
        tile_min_x = max(0, int((32 - max_x / 533.333333333)) - 1)
        tile_max_x = min(63, int((32 - min_x / 533.333333333)) + 1)
        tile_min_y = max(0, int((32 - max_y / 533.333333333)) - 1)
        tile_max_y = min(63, int((32 - min_y / 533.333333333)) + 1)
        for tile_y in range(tile_min_y, tile_max_y + 1):
            for tile_x in range(tile_min_x, tile_max_x + 1):
                tile = self._tile(tile_x, tile_y)
                if tile is None:
                    continue
                for spawn in tile.spawns:
                    if self._spawn_blocks(spawn, start, end, min_x, max_x,
                                          min_y, max_y):
                        return False
        for spawn in self._global_spawns():
            if self._spawn_blocks(spawn, start, end, min_x, max_x,
                                  min_y, max_y):
                return False
        return True


def vmaps_available(data: Optional[ClientData]) -> bool:
    return bool(data and os.path.isdir(data.vmaps_dir)
                and any(name.endswith((".vmtile", ".vmtree"))
                        for name in os.listdir(data.vmaps_dir)))
