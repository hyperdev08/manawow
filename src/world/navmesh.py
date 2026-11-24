"""Detour navigation-mesh reader and A* wayfinding.

The 3.3.5a ``.mmtile`` files are standard Recast/Detour tile blobs wrapped in
TrinityCore's 20-byte ``MmapTileHeader``.  We decode the polygon mesh (outer
vertices + neighbours) and ignore the detail mesh: it is only needed for exact
height queries, and the client is authoritative about player movement.

Coordinates are converted from Detour's Y-up space back to WoW's Z-up space::

    world = (detour.z, detour.x, detour.y)

A* runs over polygon centres.  Adjacency comes from Detour's internal
``neis`` array inside a tile, shared portal edges between tiles, and parsed
off-mesh links, so a path can cross tile boundaries without knowing Detour's
packed poly-ref encoding.  The corridor is converted to a taut path with
Detour's funnel/string-pulling algorithm.
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
# TrinityCore's generated navmesh areas (src/common/Collision/Maps/MapDefines.h).
# Older community mmap sets use area 1/flag 1 for normal ground; both are
# accepted so existing extracted data keeps working.
NAV_AREA_GROUND = 11
NAV_AREA_GROUND_STEEP = 10
NAV_AREA_WATER = 9
NAV_AREA_MAGMA_SLIME = 8
NAV_AREA_MAX_VALUE = NAV_AREA_GROUND
NAV_GROUND = 1 << (NAV_AREA_MAX_VALUE - NAV_AREA_GROUND)
NAV_GROUND_STEEP = 1 << (NAV_AREA_MAX_VALUE - NAV_AREA_GROUND_STEEP)
NAV_WATER = 1 << (NAV_AREA_MAX_VALUE - NAV_AREA_WATER)
NAV_MAGMA_SLIME = 1 << (NAV_AREA_MAX_VALUE - NAV_AREA_MAGMA_SLIME)
NAV_GROUND_FLAGS = NAV_GROUND | NAV_GROUND_STEEP
NAV_ALL_FLAGS = NAV_GROUND | NAV_GROUND_STEEP | NAV_WATER | NAV_MAGMA_SLIME
# A path may cross several tiles.  The old cache held only 32 tiles, while a
# single 5x5 endpoint neighbourhood can load up to 50, so endpoint tiles were
# evicted before A* ran.  Keep enough corridor tiles and cap the straight-line
# corridor loader so very long map crossings do not load the whole map.
MAX_CORRIDOR_TILES = 64
# Long routes are solved in overlapping eight-tile chunks so the cache never
# needs to hold an entire cross-continent corridor.
PATH_CHUNK_TILES = 8
# Polygons are indexed into 16-yard cells so nearest-point queries do not have
# to walk every polygon of every loaded tile.  Creature wander, line-of-sight
# fallbacks and path endpoint snapping all depend on this query.
NAV_BUCKET_SIZE = 16.0


def poly_terrain_flag(poly: NavPoly) -> int:
    """Map a Detour area (or legacy flag) to TrinityCore's NavTerrainFlag."""
    if NAV_AREA_MAGMA_SLIME <= poly.area <= NAV_AREA_GROUND:
        return 1 << (NAV_AREA_MAX_VALUE - poly.area)
    if poly.flags:
        return int(poly.flags)
    # Community extracts commonly use area 1 + flag 1 for normal ground.
    return NAV_GROUND


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
    flags: int = 0
    nav_tile: Optional["NavTile"] = field(default=None, repr=False)
    detail_index: int = -1

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
    detail_meshes: list[tuple[int, int, int, int]] = field(default_factory=list)
    detail_verts: list[tuple[float, float, float]] = field(default_factory=list)
    detail_tris: list[tuple[int, int, int, int]] = field(default_factory=list)
    offmesh: list[tuple[tuple[float, float, float],
                        tuple[float, float, float], bool]] = field(default_factory=list)
    buckets: dict[tuple[int, int], list[NavPoly]] = field(default_factory=dict)
    bucket_complete: bool = True


def _build_poly_buckets(tile: NavTile) -> None:
    """Spatial hash of walkable polygons for nearest-point queries."""
    buckets: dict[tuple[int, int], list[NavPoly]] = {}
    size = NAV_BUCKET_SIZE
    for poly in tile.polys:
        if not poly.walkable:
            continue
        bx0 = int(math.floor(poly.min_x / size))
        bx1 = int(math.floor(poly.max_x / size))
        by0 = int(math.floor(poly.min_y / size))
        by1 = int(math.floor(poly.max_y / size))
        if (bx1 - bx0 + 1) * (by1 - by0 + 1) > 256:
            # A pathological polygon would cover most of the tile; fall back
            # to the exact full scan instead of filling thousands of cells.
            tile.buckets = {}
            tile.bucket_complete = False
            return
        for bx in range(bx0, bx1 + 1):
            for by in range(by0, by1 + 1):
                buckets.setdefault((bx, by), []).append(poly)
    tile.buckets = buckets
    tile.bucket_complete = True


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


def _triangle_height(point: tuple[float, float],
                     a: tuple[float, float, float],
                     b: tuple[float, float, float],
                     c: tuple[float, float, float]) -> Optional[float]:
    """Height of the triangle at ``point``, or ``None`` when outside it."""
    denom = ((b[1] - c[1]) * (a[0] - c[0])
             + (c[0] - b[0]) * (a[1] - c[1]))
    if abs(denom) <= 1e-12:
        return None
    l1 = ((b[1] - c[1]) * (point[0] - c[0])
          + (c[0] - b[0]) * (point[1] - c[1])) / denom
    l2 = ((c[1] - a[1]) * (point[0] - c[0])
          + (a[0] - c[0]) * (point[1] - c[1])) / denom
    l3 = 1.0 - l1 - l2
    if l1 < -1e-6 or l2 < -1e-6 or l3 < -1e-6:
        return None
    return l1 * a[2] + l2 * b[2] + l3 * c[2]


def _polygon_height(poly: NavPoly, x: float, y: float) -> float:
    # Detour stores a detail sub-mesh (unique vertices + triangles) for exact
    # heights.  Using it matters on stairs, ramps and uneven WMO floors; the
    # base polygon plane can be several yards off there.
    tile = poly.nav_tile
    if (tile is not None and tile.detail_meshes and tile.detail_tris
            and 0 <= int(poly.detail_index) < len(tile.detail_meshes)):
        vert_base, tri_base, _vert_count, tri_count = \
            tile.detail_meshes[int(poly.detail_index)]
        base_count = len(poly.verts)
        for index in range(int(tri_count)):
            tri_index = int(tri_base) + index
            if tri_index >= len(tile.detail_tris):
                break
            a_i, b_i, c_i, _flags = tile.detail_tris[tri_index]
            points = []
            for vertex_index in (a_i, b_i, c_i):
                if vertex_index < base_count:
                    points.append(poly.verts[vertex_index])
                else:
                    detail_index = int(vert_base) + vertex_index - base_count
                    if detail_index >= len(tile.detail_verts):
                        points = []
                        break
                    points.append(tile.detail_verts[detail_index])
            if len(points) == 3:
                height = _triangle_height((x, y), *points)
                if height is not None:
                    return height
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
            try:
                poly_verts = [verts[raw_verts[i]]
                              for i in range(vert_count_poly)]
            except IndexError:
                continue
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
                int(_flags),
            )
            polys.append(poly)
    except struct.error:
        return None

    # Optional detail sub-meshes give exact polygon heights.  Layout follows
    # dtNavMesh::serialize: links, detail meshes, detail vertices, detail
    # triangles, BVH nodes, then off-mesh connections.
    detail_meshes: list[tuple[int, int, int, int]] = []
    detail_verts: list[tuple[float, float, float]] = []
    detail_tris: list[tuple[int, int, int, int]] = []
    offmesh: list[tuple[tuple[float, float, float],
                        tuple[float, float, float], bool]] = []
    try:
        # TC 3.3.5 builds with DT_POLYREF64 (16-byte dtLink), but some
        # community extracts use 32-bit refs (12-byte dtLink).  Pick the
        # layout that matches the serialized tile length.
        remaining = len(data) - cursor
        fixed_size = (max(0, detail_mesh_count) * 12
                      + max(0, detail_vert_count) * 12
                      + max(0, detail_tri_count) * 4
                      + max(0, bv_node_count) * 16
                      + max(0, offmesh_count) * 36)
        link_size = 16
        if max(0, link_count) > 0:
            size16 = max(0, link_count) * 16 + fixed_size
            size12 = max(0, link_count) * 12 + fixed_size
            if abs(remaining - size12) < abs(remaining - size16):
                link_size = 12
        cursor += int(max(0, link_count)) * link_size
        for _index in range(max(0, detail_mesh_count)):
            vert_base, tri_base, vert_count_d, tri_count_d = \
                struct.unpack_from("<IIBB", data, cursor)
            cursor += 12
            detail_meshes.append((int(vert_base), int(tri_base),
                                  int(vert_count_d), int(tri_count_d)))
        if detail_vert_count > 0:
            raw_detail = struct.unpack_from(
                "<%df" % (detail_vert_count * 3), data, cursor)
            cursor += detail_vert_count * 12
            for index in range(detail_vert_count):
                dx, dy, dz = raw_detail[index * 3:index * 3 + 3]
                detail_verts.append((float(dz), float(dx), float(dy)))
        for _index in range(max(0, detail_tri_count)):
            a, b, c, flags = struct.unpack_from("<4B", data, cursor)
            cursor += 4
            detail_tris.append((int(a), int(b), int(c), int(flags)))
        cursor += max(0, bv_node_count) * 16
        for _index in range(max(0, offmesh_count)):
            raw = struct.unpack_from("<7fHBBI", data, cursor)
            cursor += 36
            offmesh.append((
                detour_to_world(raw[0:3]), detour_to_world(raw[3:6]),
                bool(int(raw[8]) & 1)))
    except struct.error:
        # A truncated optional section must not discard the base mesh.
        detail_meshes = []
        detail_verts = []
        detail_tris = []
        offmesh = []

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
    tile = NavTile(int(tile_x), int(tile_y), verts, polys, bmin, bmax, edges,
                   {poly.index: poly for poly in polys}, detail_meshes,
                   detail_verts, detail_tris, offmesh)
    for poly in polys:
        poly.nav_tile = tile
        poly.detail_index = poly.index
    _build_poly_buckets(tile)
    return tile


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


def _tri_area2(a, b, c) -> float:
    """Detour's dtTriArea2D using WoW's X/Y ground plane."""
    return ((c[0] - a[0]) * (b[1] - a[1])
            - (b[0] - a[0]) * (c[1] - a[1]))


def _same_point(a, b, epsilon: float = 1e-3) -> bool:
    return (abs(a[0] - b[0]) <= epsilon
            and abs(a[1] - b[1]) <= epsilon
            and abs(a[2] - b[2]) <= epsilon)


def _funnel(start, goal, portals) -> list:
    """Detour-style string pulling through oriented portal edges.

    ``portals`` contains ``(left, right)`` points in travel order, already
    oriented relative to the direction of travel.  The result is the taut
    path around the corridor corners instead of one midpoint per portal.
    """
    if not portals:
        return [start, goal] if not _same_point(start, goal) else [start]
    points = [start]
    apex = start
    left = start
    right = start
    apex_index = 0
    left_index = 0
    right_index = 0
    final_portals = list(portals) + [(goal, goal)]
    index = 0
    while index < len(final_portals):
        left_point, right_point = final_portals[index]
        if _tri_area2(apex, right, right_point) <= 0.0:
            if _same_point(apex, right) \
                    or _tri_area2(apex, left, right_point) > 0.0:
                right = right_point
                right_index = index
            else:
                if not _same_point(points[-1], left):
                    points.append(left)
                apex = left
                apex_index = left_index
                left = right = apex
                left_index = right_index = apex_index
                index = apex_index + 1
                continue
        if _tri_area2(apex, left, left_point) >= 0.0:
            if _same_point(apex, left) \
                    or _tri_area2(apex, right, left_point) < 0.0:
                left = left_point
                left_index = index
            else:
                if not _same_point(points[-1], right):
                    points.append(right)
                apex = right
                apex_index = right_index
                left = right = apex
                left_index = right_index = apex_index
                index = apex_index + 1
                continue
        index += 1
    if not _same_point(points[-1], goal):
        points.append(goal)
    return points


def _smooth_path(start, goal, path_polys: list[NavPoly],
                 start_poly: NavPoly, goal_poly: NavPoly,
                 offmesh_edges=None) -> list:
    """Turn a polygon corridor into a taut portal/jump path.

    The funnel is Detour's string-pulling algorithm in the X/Y plane; Z is
    taken from the portal endpoint.  Off-mesh links split the corridor and
    contribute their actual jump endpoints.
    """
    if not path_polys:
        return [start, goal]
    offmesh_edges = offmesh_edges or {}
    snapped = closest_on_poly(goal, goal_poly)
    goal_point = (snapped[1], snapped[2], snapped[3])
    points = [start]
    segment_start = start
    portals = []
    for first, second in zip(path_polys, path_polys[1:]):
        jump = offmesh_edges.get((first.key, second.key))
        if jump is not None:
            entry, exit_point = jump
            segment = _funnel(segment_start, entry, portals)
            for point in segment[1:]:
                if not _same_point(points[-1], point):
                    points.append(point)
            if not _same_point(points[-1], exit_point):
                points.append(exit_point)
            segment_start = exit_point
            portals = []
            continue
        portal = _shared_portal(first, second)
        if portal is not None:
            # Orient the shared edge left/right along the travel direction.
            dx = second.center[0] - first.center[0]
            dy = second.center[1] - first.center[1]
            a, b = portal
            cross_a = (dx * (a[1] - first.center[1])
                       - dy * (a[0] - first.center[0]))
            cross_b = (dx * (b[1] - first.center[1])
                       - dy * (b[0] - first.center[0]))
            if cross_a >= cross_b:
                portals.append((a, b))
            else:
                portals.append((b, a))
        else:
            # Geometric matching failed at a degenerate junction; a zero-width
            # portal keeps the funnel inside the corridor.
            midpoint = ((first.center[0] + second.center[0]) * 0.5,
                        (first.center[1] + second.center[1]) * 0.5,
                        (first.center[2] + second.center[2]) * 0.5)
            portals.append((midpoint, midpoint))
    final = _funnel(segment_start, goal_point, portals)
    for point in final[1:]:
        if not _same_point(points[-1], point):
            points.append(point)
    return points


class NavMeshMap:
    """Lazy tile cache plus A* pathfinding for one map."""

    def __init__(self, data: ClientData, map_id: int, tile_cache_limit: int = 64):
        self.data = data
        self.map_id = int(map_id)
        self.tile_cache_limit = max(4, int(tile_cache_limit))
        self._tiles: dict[tuple[int, int], Optional[NavTile]] = {}
        self._order: list[tuple[int, int]] = []
        self._graph: dict[tuple[int, int, int],
                          list[tuple[tuple[int, int, int], float]]] = {}
        self._graph_dirty = True
        self._offmesh_edges: dict[
            tuple[tuple[int, int, int], tuple[int, int, int]],
            tuple[tuple[float, float, float], tuple[float, float, float]]] = {}
        self._chunking = False

    # -- tile cache ----------------------------------------------------
    def _tile(self, tile_x: int, tile_y: int) -> Optional[NavTile]:
        key = (int(tile_x), int(tile_y))
        if key in self._tiles:
            # LRU: keep tiles that are actually used by recent paths.
            try:
                self._order.remove(key)
            except ValueError:
                pass
            self._order.append(key)
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

    def _line_tiles(self, start, end) -> list[tuple[int, int]]:
        """Bresenham tile line from start to end."""
        x0, y0 = grid_coord(start[0]), grid_coord(start[1])
        x1, y1 = grid_coord(end[0]), grid_coord(end[1])
        dx, dy = abs(x1 - x0), abs(y1 - y0)
        sx = 1 if x0 < x1 else -1
        sy = 1 if y0 < y1 else -1
        err = dx - dy
        tiles: list[tuple[int, int]] = []
        while True:
            tiles.append((x0, y0))
            if x0 == x1 and y0 == y1:
                break
            e2 = 2 * err
            if e2 > -dy:
                err -= dy
                x0 += sx
            if e2 < dx:
                err += dx
                y0 += sy
        return tiles

    def _ensure_corridor(self, start, end, margin: int = 1) -> bool:
        """Load tiles along the straight start->end corridor.

        The first A* attempt uses only the endpoint tiles, which is cheap for
        a short chase.  If it fails, the path may simply be missing the tiles
        between the endpoints.  Loading them before retrying mirrors the
        reference core, where the map's navmesh is available as a whole.
        Returns ``False`` when the corridor is too long to load safely.
        """
        tiles = self._line_tiles(start, end)
        if len(tiles) > MAX_CORRIDOR_TILES:
            return False
        required: set[tuple[int, int]] = set()
        for tile_x, tile_y in tiles:
            for oy in range(-margin, margin + 1):
                for ox in range(-margin, margin + 1):
                    tx, ty = tile_x + ox, tile_y + oy
                    if 0 <= tx <= 63 and 0 <= ty <= 63:
                        required.add((tx, ty))
        # Never load more tiles than the cache can retain: evicting the start
        # of the corridor before A* runs would just recreate the original bug.
        if len(required) > self.tile_cache_limit:
            return False
        for tx, ty in required:
            self._tile(tx, ty)
        # Touch the whole corridor again so the LRU cannot evict its start
        # while the rest was still being parsed.
        for tx, ty in required:
            self._tile(tx, ty)
        return True

    def _clear_tiles(self) -> None:
        self._tiles.clear()
        self._order.clear()
        self._graph.clear()
        self._offmesh_edges.clear()
        self._graph_dirty = True

    def _path_chunked(self, start, end, include_flags: int = NAV_GROUND_FLAGS):
        """Path across more tiles than the cache can retain at once.

        The corridor is split into overlapping chunks; each chunk is A*'d
        normally, its waypoints retained, then its tiles are released before
        the next chunk.  This avoids both the old hard cap and an unbounded
        memory spike when a long route crosses the map.
        """
        line = self._line_tiles(start, end)
        if len(line) <= PATH_CHUNK_TILES:
            return None
        self._clear_tiles()
        points = [start]
        current = start
        total = len(line)
        chunk = PATH_CHUNK_TILES
        index = 0
        self._chunking = True
        try:
            while index < total - 1:
                next_index = min(total - 1, index + chunk + 1)
                if next_index >= total - 1:
                    target = end
                else:
                    fraction = next_index / max(1, total - 1)
                    target = (start[0] + (end[0] - start[0]) * fraction,
                              start[1] + (end[1] - start[1]) * fraction,
                              start[2] + (end[2] - start[2]) * fraction)
                chunk_path = self.find_path(
                    current, target, include_flags=include_flags)
                if chunk_path is None:
                    return None
                for point in chunk_path[1:]:
                    if not _same_point(points[-1], point):
                        points.append(point)
                current = points[-1]
                index = next_index
                # Release this chunk's tiles before loading the next one.
                self._clear_tiles()
        finally:
            self._chunking = False
        return points if len(points) >= 2 else None

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
        self._offmesh_edges = {}
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
        # Off-mesh connections are jumps/links, not normal walkable polygons.
        # Connect the walkable polygons nearest their two endpoints directly.
        for tile in self._tiles.values():
            if tile is None:
                continue
            for start, end, bidir in tile.offmesh:
                start_found = self._nearest_poly(
                    start, max_distance=8.0, include_flags=NAV_ALL_FLAGS)
                end_found = self._nearest_poly(
                    end, max_distance=8.0, include_flags=NAV_ALL_FLAGS)
                if start_found is None or end_found is None:
                    continue
                start_poly, _ = start_found
                end_poly, _ = end_found
                if start_poly.key == end_poly.key:
                    continue
                cost = max(0.1, math.dist(start, end))
                graph.setdefault(start_poly.key, []).append(
                    (end_poly.key, cost))
                if bidir:
                    graph.setdefault(end_poly.key, []).append(
                        (start_poly.key, cost))
                self._offmesh_edges[
                    (start_poly.key, end_poly.key)] = (start, end)
                if bidir:
                    self._offmesh_edges[
                        (end_poly.key, start_poly.key)] = (end, start)
        self._graph = graph
        self._graph_dirty = False

    def _poly(self, key: tuple[int, int, int]) -> Optional[NavPoly]:
        tile = self._tiles.get((key[0], key[1]))
        if tile is None:
            return None
        return tile.by_index.get(key[2])

    def _nearest_poly(self, point, max_distance: float = 12.0,
                      use_z: bool = True,
                      include_flags: int = NAV_GROUND_FLAGS):
        best = None
        best_distance = None
        best_poly = None
        size = NAV_BUCKET_SIZE
        # Only inspect polygons whose 16-yard cells overlap the query square.
        # Falling back to every polygon preserves the old behaviour for tiles
        # whose spatial index could not be built.
        bx0 = int(math.floor((point[0] - max_distance) / size))
        bx1 = int(math.floor((point[0] + max_distance) / size))
        by0 = int(math.floor((point[1] - max_distance) / size))
        by1 = int(math.floor((point[1] + max_distance) / size))
        bucket_search = ((bx1 - bx0 + 1) * (by1 - by0 + 1) <= 1024)
        for tile in self._tiles.values():
            if tile is None:
                continue
            candidates = None
            if bucket_search and tile.buckets:
                # A polygon spanning several cells appears in each; dedupe by
                # index and keep index order for deterministic tie-breaking.
                by_index: dict[int, NavPoly] = {}
                for bx in range(bx0, bx1 + 1):
                    for by in range(by0, by1 + 1):
                        for poly in tile.buckets.get((bx, by), ()):
                            by_index[poly.index] = poly
                candidates = [by_index[index] for index in sorted(by_index)]
            if candidates is None:
                candidates = tile.polys
            for poly in candidates:
                if not poly.walkable:
                    continue
                if not (poly_terrain_flag(poly) & include_flags):
                    continue
                # Cheap bounding-box reject before the edge loop.
                if (point[0] < poly.min_x - max_distance
                        or point[0] > poly.max_x + max_distance
                        or point[1] < poly.min_y - max_distance
                        or point[1] > poly.max_y + max_distance):
                    continue
                result = closest_on_poly(point, poly)
                distance = result[0]
                if use_z:
                    # Multi-level WMOs stack polygons directly above each
                    # other; prefer the floor nearest the unit's actual height.
                    distance = math.hypot(distance, result[3] - point[2])
                if best_distance is None or distance < best_distance:
                    best = result
                    best_distance = distance
                    best_poly = poly
        if best is None or best_distance > max_distance:
            return None
        return best_poly, (best[1], best[2], best[3])

    def _astar(self, start_key, goal_key, goal_point,
               include_flags: int = NAV_GROUND_FLAGS):
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
                if not (poly_terrain_flag(neighbour_poly) & include_flags):
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
                      use_z: bool = True,
                      include_flags: int = NAV_GROUND_FLAGS):
        self._tile_at(point[0], point[1])
        found = self._nearest_poly(point, max_distance, use_z=use_z,
                                   include_flags=include_flags)
        if found is None:
            self._ensure_around(point[0], point[1], 1)
            self._tile_at(point[0], point[1])
            found = self._nearest_poly(point, max_distance, use_z=use_z,
                                       include_flags=include_flags)
        if found is None:
            return None
        poly, snapped = found
        return snapped

    def random_point(self, center, radius: float,
                     rng: Optional[_random.Random] = None,
                     include_flags: int = NAV_GROUND_FLAGS):
        rng = rng or _random
        for _ in range(16):
            angle = rng.uniform(0.0, 2.0 * math.pi)
            distance = rng.uniform(max(0.5, radius * 0.25), max(0.5, radius))
            candidate = (center[0] + math.cos(angle) * distance,
                         center[1] + math.sin(angle) * distance,
                         center[2])
            snapped = self.point_on_mesh(
                candidate, max_distance=4.0, include_flags=include_flags)
            if snapped is not None:
                return snapped
        return None

    def find_path(self, start, end, max_tiles: int = 2,
                  include_flags: int = NAV_GROUND_FLAGS):
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
        start_found = self._nearest_poly(start, include_flags=include_flags)
        goal_found = self._nearest_poly(end, include_flags=include_flags)
        if start_found is None or goal_found is None:
            self._ensure_around(start[0], start[1], 1)
            self._ensure_around(end[0], end[1], 1)
            self._tile_at(start[0], start[1])
            self._tile_at(end[0], end[1])
            self._rebuild_graph()
            start_found = self._nearest_poly(start, include_flags=include_flags)
            goal_found = self._nearest_poly(end, include_flags=include_flags)
        if start_found is None or goal_found is None:
            return None
        start_poly, snapped_start = start_found
        goal_poly, snapped_goal = goal_found
        path_keys = self._astar(start_poly.key, goal_poly.key, snapped_goal, include_flags)
        if path_keys is None:
            # The endpoints can be connected through tiles that were never
            # loaded (only their own tiles were requested).  Load the direct
            # corridor and retry before falling back to endpoint radius growth.
            corridor_ok = self._ensure_corridor(start, end)
            if corridor_ok:
                self._rebuild_graph()
                start_found = self._nearest_poly(start, include_flags=include_flags)
                goal_found = self._nearest_poly(end, include_flags=include_flags)
                if start_found is not None and goal_found is not None:
                    start_poly, snapped_start = start_found
                    goal_poly, snapped_goal = goal_found
                    path_keys = self._astar(
                        start_poly.key, goal_poly.key, snapped_goal,
                        include_flags)
            elif not getattr(self, "_chunking", False) \
                    and len(self._line_tiles(start, end)) > PATH_CHUNK_TILES:
                chunked = self._path_chunked(start, end, include_flags)
                if chunked is not None:
                    return chunked
        if path_keys is None:
            for radius in range(2, max_tiles + 1):
                self._ensure_around(start[0], start[1], radius)
                self._ensure_around(end[0], end[1], radius)
                # Keep the endpoint tiles MRU: the 5x5 neighbourhoods can
                # otherwise push them out of a full cache before the search.
                self._tile_at(start[0], start[1])
                self._tile_at(end[0], end[1])
                self._rebuild_graph()
                start_found = self._nearest_poly(start, include_flags=include_flags)
                goal_found = self._nearest_poly(end, include_flags=include_flags)
                if start_found is None or goal_found is None:
                    continue
                start_poly, snapped_start = start_found
                goal_poly, snapped_goal = goal_found
                path_keys = self._astar(start_poly.key, goal_poly.key, snapped_goal, include_flags)
                if path_keys is not None:
                    break
        if path_keys is None:
            return None
        path_polys = [self._poly(key) for key in path_keys]
        path_polys = [poly for poly in path_polys if poly is not None]
        if len(path_polys) <= 1:
            return [start, (snapped_goal[0], snapped_goal[1], snapped_goal[2])]
        smoothed = _smooth_path(start, end, path_polys, start_poly, goal_poly,
                                self._offmesh_edges)
        # Guarantee the final point is a snapped point on the goal polygon.
        if not smoothed:
            smoothed = [start, snapped_goal]
        return smoothed


def navmesh_available(data: Optional[ClientData]) -> bool:
    return bool(data and os.path.isdir(data.mmaps_dir)
                and any(name.endswith(".mmtile")
                        for name in os.listdir(data.mmaps_dir)))
