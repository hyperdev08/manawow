"""Detour navigation-mesh reader and A* wayfinding.

The 3.3.5a ``.mmtile`` files are standard Recast/Detour tile blobs wrapped in
TrinityCore's 20-byte ``MmapTileHeader``.  We decode the polygon mesh (outer
vertices + neighbours) and ignore the detail mesh: it is only needed for exact
height queries, and the client is authoritative about player movement.

Coordinates are converted from Detour's Y-up space back to WoW's Z-up space::

    world = (detour.z, detour.x, detour.y)

A* runs over polygon centres.  Adjacency comes from Detour's internal
``neis`` array inside a tile and from shared portal edges between tiles, so a
path can cross tile boundaries without knowing Detour's packed poly-ref
encoding.  The resulting corridor is string-pulled by sampling against the
polygon union, which removes the characteristic "walk through every triangle
centre" look.
"""
from __future__ import annotations

import heapq
import math
import os
import random as _random
import struct
from dataclasses import dataclass, field
from typing import Optional

from .client_data import ClientData, grid_coord

MMAP_MAGIC = 0x4D4D4150          # 'MMAP' little endian
DT_MESH_MAGIC = 0x444E4156       # 'VAND'
DT_NAVMESH_VERSION = 7
MMAP_HEADER_SIZE = 20
DT_MESH_HEADER_SIZE = 100
DT_VERTS_PER_POLYGON = 6
DT_EXT_LINK = 0x8000
DT_POLY_TYPE_OFFMESH = 1
MAX_PATH_NODES = 120000
PORTAL_EPSILON = 0.08


@dataclass
class NavPoly:
    tile_x: int
    tile_y: int
    index: int
    verts: list[tuple[float, float, float]]
    neis: tuple[int, ...]
    area: int
    poly_type: int
    center: tuple[float, float, float]
    min_x: float = 0.0
    max_x: float = 0.0
    min_y: float = 0.0
    max_y: float = 0.0
    walkable: bool = True

    @property
    def key(self) -> tuple[int, int, int]:
        return (self.tile_x, self.tile_y, self.index)


@dataclass
class NavTile:
    tile_x: int
    tile_y: int
    verts: list[tuple[float, float, float]]
    polys: list[NavPoly]
    bmin: tuple[float, float, float]
    bmax: tuple[float, float, float]
    edges: list[tuple[tuple[int, int], tuple[tuple[float, float, float],
                                                 tuple[float, float, float]]]] = field(default_factory=list)
    by_index: dict[int, NavPoly] = field(default_factory=dict)


def point_in_polygon(point: tuple[float, float],
                     verts: list[tuple[float, float, float]],
                     epsilon: float = 1e-5) -> bool:
    """Convex-polygon containment test (navmesh polygons are convex)."""
    sign = 0
    count = len(verts)
    for index in range(count):
        a = verts[index]
        b = verts[(index + 1) % count]
        cross = ((b[0] - a[0]) * (point[1] - a[1])
                 - (b[1] - a[1]) * (point[0] - a[0]))
        if abs(cross) <= epsilon:
            continue
        current = 1 if cross > 0 else -1
        if sign == 0:
            sign = current
        elif sign != current:
            return False
    return True


def closest_point_on_segment(point, a, b):
    dx = b[0] - a[0]
    dy = b[1] - a[1]
    length_sq = dx * dx + dy * dy
    if length_sq <= 1e-12:
        return (a[0], a[1], a[2])
    t = ((point[0] - a[0]) * dx + (point[1] - a[1]) * dy) / length_sq
    t = max(0.0, min(1.0, t))
    return (a[0] + dx * t, a[1] + dy * t, a[2] + (b[2] - a[2]) * t)


def closest_on_poly(point: tuple[float, float, float],
                    poly: NavPoly) -> tuple[float, float, float, float]:
    """Return ``(distance, x, y, z)`` for the nearest point of a polygon."""
    if point_in_polygon((point[0], point[1]), poly.verts):
        return (0.0, point[0], point[1], _polygon_height(poly, point[0],
                                                         point[1]))
    best = None
    count = len(poly.verts)
    for index in range(count):
        candidate = closest_point_on_segment(
            point, poly.verts[index], poly.verts[(index + 1) % count])
        distance = math.hypot(candidate[0] - point[0],
                              candidate[1] - point[1])
        if best is None or distance < best[0]:
            best = (distance, candidate[0], candidate[1], candidate[2])
    return best or (1.0e9, point[0], point[1], point[2])


def _polygon_height(poly: NavPoly, x: float, y: float) -> float:
    # Fit a plane through the first non-collinear triangle.  Navmesh polygons
    # are small and nearly planar.
    verts = poly.verts
    if len(verts) >= 3:
        a = verts[0]
        for index in range(1, len(verts) - 1):
            b = verts[index]
            c = verts[index + 1]
            ux, uy, uz = b[0] - a[0], b[1] - a[1], b[2] - a[2]
            vx, vy, vz = c[0] - a[0], c[1] - a[1], c[2] - a[2]
            nx = uy * vz - uz * vy
            ny = uz * vx - ux * vz
            nz = ux * vy - uy * vx
            if abs(nz) > 1e-8:
                return a[2] - (nx * (x - a[0]) + ny * (y - a[1])) / nz
    return sum(v[2] for v in verts) / max(1, len(verts))


def _center(verts: list[tuple[float, float, float]]
            ) -> tuple[float, float, float]:
    count = max(1, len(verts))
    return (sum(v[0] for v in verts) / count,
            sum(v[1] for v in verts) / count,
            sum(v[2] for v in verts) / count)


def parse_navmesh_tile(path: str, tile_x: int, tile_y: int
                       ) -> Optional[NavTile]:
    try:
        with open(path, "rb") as handle:
            data = handle.read()
    except OSError:
        return None
    if len(data) < MMAP_HEADER_SIZE + DT_MESH_HEADER_SIZE:
        return None
    try:
        magic, dt_version, mmap_version, payload_size, uses_liquids = (
            struct.unpack_from("<IIIIB", data, 0))
    except struct.error:
        return None
    if magic != MMAP_MAGIC or dt_version != DT_NAVMESH_VERSION:
        return None
    if payload_size and len(data) < MMAP_HEADER_SIZE + payload_size:
        return None

    cursor = MMAP_HEADER_SIZE
    try:
        (mesh_magic, version, _hx, _hy, _layer, _user,
         poly_count, vert_count, link_count, detail_mesh_count,
         detail_vert_count, detail_tri_count, bv_node_count,
         offmesh_count, offmesh_base) = struct.unpack_from("<15i", data,
                                                           cursor)
        walk_height, walk_radius, walk_climb = struct.unpack_from(
            "<3f", data, cursor + 60)
        bmin_d = struct.unpack_from("<3f", data, cursor + 72)
        bmax_d = struct.unpack_from("<3f", data, cursor + 84)
    except struct.error:
        return None
    if mesh_magic != DT_MESH_MAGIC or version != DT_NAVMESH_VERSION:
        return None
    if (poly_count < 0 or vert_count < 0 or poly_count > 300000
            or vert_count > 600000):
        return None

    def detour_to_world(v):
        return (float(v[2]), float(v[0]), float(v[1]))

    bmin = detour_to_world(bmin_d)
    bmax = detour_to_world(bmax_d)
    cursor += DT_MESH_HEADER_SIZE

    verts: list[tuple[float, float, float]] = []
    try:
        raw = struct.unpack_from("<%df" % (vert_count * 3), data, cursor)
        cursor += vert_count * 12
        for index in range(vert_count):
            dx, dy, dz = raw[index * 3:index * 3 + 3]
            verts.append((float(dz), float(dx), float(dy)))
    except struct.error:
        return None

    polys: list[NavPoly] = []
    try:
        for index in range(poly_count):
            _first_link, = struct.unpack_from("<I", data, cursor)
            raw_verts = struct.unpack_from("<%dH" % DT_VERTS_PER_POLYGON,
                                           data, cursor + 4)
            raw_neis = struct.unpack_from("<%dH" % DT_VERTS_PER_POLYGON,
                                          data, cursor + 16)
            _flags, = struct.unpack_from("<H", data, cursor + 28)
            vert_count_poly, area_and_type = struct.unpack_from(
                "<2B", data, cursor + 30)
            cursor += 32
            if vert_count_poly < 3 or vert_count_poly > DT_VERTS_PER_POLYGON:
                continue
            poly_verts = [verts[raw_verts[i]]
                          for i in range(vert_count_poly)]
            center = _center(poly_verts)
            area = int(area_and_type & 0x3F)
            poly_type = int(area_and_type >> 6)
            poly = NavPoly(
                int(tile_x), int(tile_y), index, poly_verts,
                tuple(int(v) for v in raw_neis[:vert_count_poly]),
                area, poly_type, center,
                min(v[0] for v in poly_verts),
                max(v[0] for v in poly_verts),
                min(v[1] for v in poly_verts),
                max(v[1] for v in poly_verts),
                not (poly_type == DT_POLY_TYPE_OFFMESH or area == 0),
            )
            polys.append(poly)
    except struct.error:
        return None

    # Build the portal edge list used for cross-tile adjacency.
    edges = []
    for poly in polys:
        if not poly.walkable:
            continue
        count = len(poly.verts)
        for index in range(count):
            a = poly.verts[index]
            b = poly.verts[(index + 1) % count]
            endpoint_a = (round(a[0], 2), round(a[1], 2), round(a[2], 2))
            endpoint_b = (round(b[0], 2), round(b[1], 2), round(b[2], 2))
            # Canonical orientation so the two tiles store the same key even
            # when they list the shared portal in opposite directions.
            key = tuple(sorted((endpoint_a, endpoint_b)))
            edges.append((poly.key, key))
    return NavTile(int(tile_x), int(tile_y), verts, polys, bmin, bmax, edges,
                   {poly.index: poly for poly in polys})


def _shared_portal(first: NavPoly, second: NavPoly
                   ) -> Optional[tuple[tuple[float, float, float],
                                       tuple[float, float, float]]]:
    """Two vertices shared by both polygons, or ``None``."""
    shared = []
    for a in first.verts:
        for b in second.verts:
            if (abs(a[0] - b[0]) <= PORTAL_EPSILON
                    and abs(a[1] - b[1]) <= PORTAL_EPSILON
                    and abs(a[2] - b[2]) <= PORTAL_EPSILON):
                shared.append(a)
                break
        if len(shared) >= 2:
            return (shared[0], shared[1])
    return None


def _smooth_path(start, goal, path_polys: list[NavPoly],
                 start_poly: NavPoly, goal_poly: NavPoly) -> list:
    """Turn a polygon corridor into portal midpoints.

    Connecting the midpoints of consecutive shared edges stays inside two
    adjacent convex polygons, so the result never cuts through a wall, which
    a generic "sample the union of all corridor polygons" smoother can do when
    the corridor doubles back.  It also removes the zig-zag of following every
    polygon centre.
    """
    if not path_polys:
        return [start, goal]
    points = [start]
    for first, second in zip(path_polys, path_polys[1:]):
        portal = _shared_portal(first, second)
        if portal is not None:
            a, b = portal
            waypoint = ((a[0] + b[0]) * 0.5,
                        (a[1] + b[1]) * 0.5,
                        (a[2] + b[2]) * 0.5)
        else:
            # Geometric matching can fail at a degenerate tile edge; fall
            # back to the midpoint between the two centres.
            waypoint = ((first.center[0] + second.center[0]) * 0.5,
                        (first.center[1] + second.center[1]) * 0.5,
                        (first.center[2] + second.center[2]) * 0.5)
        if math.dist(points[-1], waypoint) > 0.05:
            points.append(waypoint)

    snapped_goal = closest_on_poly(goal, goal_poly)
    goal_point = (snapped_goal[1], snapped_goal[2], snapped_goal[3])
    if math.dist(points[-1], goal_point) > 0.05:
        points.append(goal_point)
    return points


class NavMeshMap:
    """Lazy tile cache plus A* pathfinding for one map."""

    def __init__(self, data: ClientData, map_id: int, tile_cache_limit: int = 32):
        self.data = data
        self.map_id = int(map_id)
        self.tile_cache_limit = max(4, int(tile_cache_limit))
        self._tiles: dict[tuple[int, int], Optional[NavTile]] = {}
        self._order: list[tuple[int, int]] = []
        self._graph: dict[tuple[int, int, int],
                          list[tuple[tuple[int, int, int], float]]] = {}
        self._graph_dirty = True

    # -- tile cache ----------------------------------------------------
    def _tile(self, tile_x: int, tile_y: int) -> Optional[NavTile]:
        key = (int(tile_x), int(tile_y))
        if key in self._tiles:
            return self._tiles[key]
        tile = parse_navmesh_tile(
            self.data.mmap_path(self.map_id, key[0], key[1]), key[0], key[1])
        self._tiles[key] = tile
        self._order.append(key)
        self._graph_dirty = True
        while len(self._order) > self.tile_cache_limit:
            old = self._order.pop(0)
            if old != key:
                self._tiles.pop(old, None)
        return tile

    def _tile_at(self, x: float, y: float) -> None:
        tile_x = grid_coord(x)
        tile_y = grid_coord(y)
        if 0 <= tile_x <= 63 and 0 <= tile_y <= 63:
            self._tile(tile_x, tile_y)

    def _ensure_around(self, x: float, y: float, radius: int) -> None:
        tile_x = grid_coord(x)
        tile_y = grid_coord(y)
        for dy in range(-radius, radius + 1):
            for dx in range(-radius, radius + 1):
                tx, ty = tile_x + dx, tile_y + dy
                if 0 <= tx <= 63 and 0 <= ty <= 63:
                    self._tile(tx, ty)

    def _all_polys(self):
        for tile in self._tiles.values():
            if tile is None:
                continue
            for poly in tile.polys:
                if poly.walkable:
                    yield poly

    # -- graph ---------------------------------------------------------
    def _rebuild_graph(self) -> None:
        if not self._graph_dirty:
            return
        graph: dict[tuple[int, int, int],
                    list[tuple[tuple[int, int, int], float]]] = {}
        edge_owners: dict[tuple, list[tuple[tuple[int, int, int], int]]] = {}
        for tile in self._tiles.values():
            if tile is None:
                continue
            for poly in tile.polys:
                if not poly.walkable:
                    continue
                graph.setdefault(poly.key, [])
                for edge_index, neighbour in enumerate(poly.neis):
                    if neighbour == 0 or (neighbour & DT_EXT_LINK):
                        continue
                    target_index = neighbour - 1
                    target = tile.by_index.get(target_index)
                    if target is not None:
                        if not target.walkable:
                            continue
                        cost = math.dist(poly.center, target.center)
                        graph.setdefault(target.key, [])
                        if (target.key, cost) not in graph[poly.key]:
                            graph[poly.key].append((target.key, cost))
            for node_key, edge_key in tile.edges:
                edge_owners.setdefault(edge_key, []).append((node_key, 0))
        # Connect portals whose two owners live in different tiles.  A portal
        # can be shared by more than two polygons at a T-junction; connecting
        # all cross-tile pairs is harmless for A*.
        for owners in edge_owners.values():
            if len(owners) < 2:
                continue
            for first_index in range(len(owners)):
                first_key = owners[first_index][0]
                first_tile = (first_key[0], first_key[1])
                for second_index in range(first_index + 1, len(owners)):
                    second_key = owners[second_index][0]
                    if (second_key[0], second_key[1]) == first_tile:
                        continue
                    first = self._poly(first_key)
                    second = self._poly(second_key)
                    if first is None or second is None:
                        continue
                    cost = math.dist(first.center, second.center)
                    graph.setdefault(first_key, []).append((second_key, cost))
                    graph.setdefault(second_key, []).append((first_key, cost))
        self._graph = graph
        self._graph_dirty = False

    def _poly(self, key: tuple[int, int, int]) -> Optional[NavPoly]:
        tile = self._tiles.get((key[0], key[1]))
        if tile is None:
            return None
        return tile.by_index.get(key[2])

    def _nearest_poly(self, point, max_distance: float = 12.0,
                      use_z: bool = True):
        best = None
        best_distance = None
        best_poly = None
        for poly in self._all_polys():
            # Cheap bounding-box reject before the edge loop.
            if (point[0] < poly.min_x - max_distance
                    or point[0] > poly.max_x + max_distance
                    or point[1] < poly.min_y - max_distance
                    or point[1] > poly.max_y + max_distance):
                continue
            result = closest_on_poly(point, poly)
            distance = result[0]
            if use_z:
                # Multi-level WMOs stack polygons directly above each other;
                # prefer the floor nearest the unit's actual height.
                distance = math.hypot(distance, result[3] - point[2])
            if best_distance is None or distance < best_distance:
                best = result
                best_distance = distance
                best_poly = poly
        if best is None or best_distance > max_distance:
            return None
        return best_poly, (best[1], best[2], best[3])

    def _astar(self, start_key, goal_key, goal_point):
        if start_key == goal_key:
            return [start_key]
        open_heap = []
        counter = 0
        start_poly = self._poly(start_key)
        if start_poly is None:
            return None
        heapq.heappush(open_heap,
                       (math.dist(start_poly.center, goal_point), counter,
                        start_key))
        cost = {start_key: 0.0}
        came: dict[tuple[int, int, int],
                   Optional[tuple[int, int, int]]] = {start_key: None}
        visited = set()
        expanded = 0
        while open_heap:
            _estimate, _tie, current = heapq.heappop(open_heap)
            if current in visited:
                continue
            visited.add(current)
            expanded += 1
            if current == goal_key:
                path = []
                node = current
                while node is not None:
                    path.append(node)
                    node = came[node]
                return path[::-1]
            if expanded > MAX_PATH_NODES:
                return None
            current_poly = self._poly(current)
            if current_poly is None:
                continue
            for neighbour, step_cost in self._graph.get(current, ()):
                if neighbour in visited:
                    continue
                new_cost = cost.get(current, 0.0) + step_cost
                if new_cost >= cost.get(neighbour, 1.0e30):
                    continue
                neighbour_poly = self._poly(neighbour)
                if neighbour_poly is None:
                    continue
                cost[neighbour] = new_cost
                came[neighbour] = current
                counter += 1
                estimate = new_cost + math.dist(neighbour_poly.center,
                                                goal_point)
                heapq.heappush(open_heap, (estimate, counter, neighbour))
        return None

    # -- public API ----------------------------------------------------
    def point_on_mesh(self, point, max_distance: float = 12.0,
                      use_z: bool = True):
        self._tile_at(point[0], point[1])
        found = self._nearest_poly(point, max_distance, use_z=use_z)
        if found is None:
            self._ensure_around(point[0], point[1], 1)
            found = self._nearest_poly(point, max_distance, use_z=use_z)
        if found is None:
            return None
        poly, snapped = found
        return snapped

    def random_point(self, center, radius: float,
                     rng: Optional[_random.Random] = None):
        rng = rng or _random
        for _ in range(16):
            angle = rng.uniform(0.0, 2.0 * math.pi)
            distance = rng.uniform(max(0.5, radius * 0.25), max(0.5, radius))
            candidate = (center[0] + math.cos(angle) * distance,
                         center[1] + math.sin(angle) * distance,
                         center[2])
            snapped = self.point_on_mesh(candidate, max_distance=4.0)
            if snapped is not None:
                return snapped
        return None

    def find_path(self, start, end, max_tiles: int = 2):
        """Return world-space waypoints from ``start`` to ``end`` or ``None``."""
        start = (float(start[0]), float(start[1]), float(start[2]))
        end = (float(end[0]), float(end[1]), float(end[2]))
        # The common case is a short chase inside one tile.  Loading a 3x3
        # neighbourhood up front costs a visible amount of first-use time, so
        # start with the two endpoint tiles and grow only when the graph or the
        # nearest-poly search actually needs more.
        self._tile_at(start[0], start[1])
        self._tile_at(end[0], end[1])
        self._rebuild_graph()
        start_found = self._nearest_poly(start)
        goal_found = self._nearest_poly(end)
        if start_found is None or goal_found is None:
            self._ensure_around(start[0], start[1], 1)
            self._ensure_around(end[0], end[1], 1)
            self._rebuild_graph()
            start_found = self._nearest_poly(start)
            goal_found = self._nearest_poly(end)
        if start_found is None or goal_found is None:
            return None
        start_poly, snapped_start = start_found
        goal_poly, snapped_goal = goal_found
        path_keys = self._astar(start_poly.key, goal_poly.key, end)
        if path_keys is None:
            for radius in range(2, max_tiles + 1):
                self._ensure_around(start[0], start[1], radius)
                self._ensure_around(end[0], end[1], radius)
                self._rebuild_graph()
                start_found = self._nearest_poly(start)
                goal_found = self._nearest_poly(end)
                if start_found is None or goal_found is None:
                    continue
                start_poly, snapped_start = start_found
                goal_poly, snapped_goal = goal_found
                path_keys = self._astar(start_poly.key, goal_poly.key, end)
                if path_keys is not None:
                    break
        if path_keys is None:
            return None
        path_polys = [self._poly(key) for key in path_keys]
        path_polys = [poly for poly in path_polys if poly is not None]
        if len(path_polys) <= 1:
            return [start, (snapped_goal[0], snapped_goal[1], snapped_goal[2])]
        smoothed = _smooth_path(start, end, path_polys, start_poly, goal_poly)
        # Guarantee the final point is a snapped point on the goal polygon.
        if not smoothed:
            smoothed = [start, snapped_goal]
        return smoothed


def navmesh_available(data: Optional[ClientData]) -> bool:
    return bool(data and os.path.isdir(data.mmaps_dir)
                and any(name.endswith(".mmtile")
                        for name in os.listdir(data.mmaps_dir)))
