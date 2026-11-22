"""Terrain, VMap, navmesh and path-spline regression tests.

The fixtures are tiny synthetic files in the real on-disk formats, so these
tests run in CI without downloading the multi-hundred-megabyte client extracts.
"""
from __future__ import annotations

import math
import os
import struct
import sys
import types
from pathlib import Path

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src.world import geometry, object_update
from src.world.client_data import ClientData, VMAP_COORD_OFFSET, grid_coord
from src.world.movement import pack_guid
from src.world.navmesh import DT_MESH_MAGIC, NavMeshMap, point_in_polygon
from src.world.terrain import TerrainMap, load_terrain_tile
from src.world.vmap import VMapMap, load_vmap_tile


def _write_terrain(path: Path, height: float = 20.0,
                   legacy_holes: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    height_offset = 44
    v9 = [float(height)] * (129 * 129)
    v8 = [float(height)] * (128 * 128)
    height_blob = struct.pack("<4sI2f", b"MHGT", 0, 0.0, 0.0)
    height_blob += struct.pack("<%df" % (len(v9) + len(v8)), *(v9 + v8))
    holes = b""
    holes_offset = 0
    if legacy_holes:
        holes = struct.pack("<256H", *([0xFFFF] * 256))
        holes_offset = height_offset + len(height_blob)
    header = bytearray(44)
    header[0:4] = b"MAPS"
    header[4:8] = b"v1.3"
    struct.pack_into("<I", header, 8, 12340)
    (
        struct.pack_into("<8I", header, 12,
                         0, 0,
                         height_offset, len(height_blob),
                         0, 0,
                         holes_offset, len(holes))
    )
    path.write_bytes(bytes(header) + height_blob + holes)


def _write_vmo(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    blob = bytearray()
    blob += b"VMAP_4.1"
    blob += b"WMOD" + struct.pack("<2I", 8, 1)
    blob += b"GMOD" + struct.pack("<I", 1)
    # One group covering the 10x10x10 box used by the test placement.  The
    # geometry is a wall at x=0 with a doorway between y=4 and y=6, which
    # distinguishes exact triangle collision from the group AABB.
    blob += struct.pack("<6f", -5.0, -5.0, 0.0, 5.0, 5.0, 10.0)
    blob += struct.pack("<2I", 0x521, 1)
    vertices = [
        (0.0, 0.0, 0.0), (0.0, 4.0, 0.0), (0.0, 4.0, 10.0),
        (0.0, 0.0, 10.0), (0.0, 6.0, 0.0), (0.0, 10.0, 0.0),
        (0.0, 10.0, 10.0), (0.0, 6.0, 10.0),
    ]
    triangles = [(0, 1, 2), (0, 2, 3), (4, 5, 6), (4, 6, 7)]
    blob += b"VERT" + struct.pack("<2I", 4 + len(vertices) * 12,
                                  len(vertices))
    for vertex in vertices:
        blob += struct.pack("<3f", *vertex)
    blob += b"TRIM" + struct.pack("<2I", 4 + len(triangles) * 12,
                                  len(triangles))
    for triangle in triangles:
        blob += struct.pack("<3I", *triangle)
    blob += b"MBIH" + struct.pack("<6f", -5, -5, 0, 5, 5, 10)
    blob += struct.pack("<2I", 0, 0)          # empty BIH tree + objects
    blob += b"LIQU" + struct.pack("<I", 0)
    path.write_bytes(bytes(blob))


def _write_vmtile(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Stored VMap space is (OFFSET-world_x, OFFSET-world_y, world_z).
    low = (VMAP_COORD_OFFSET - 10.0, VMAP_COORD_OFFSET - 10.0, 0.0)
    high = (VMAP_COORD_OFFSET, VMAP_COORD_OFFSET, 10.0)
    blob = bytearray()
    blob += b"VMAP_4.1" + struct.pack("<I", 1)
    blob += struct.pack("<I", 4)              # MOD_HAS_BOUND
    blob += struct.pack("<H", 0) + struct.pack("<I", 1)
    blob += struct.pack("<3f", VMAP_COORD_OFFSET - 5.0,
                        VMAP_COORD_OFFSET - 5.0, 0.0)
    blob += struct.pack("<3f", 0.0, 0.0, 0.0)
    blob += struct.pack("<f", 1.0)
    blob += struct.pack("<6f", *low, *high)
    name = b"Testwall.wmo"
    blob += struct.pack("<I", len(name)) + name + struct.pack("<I", 0)
    path.write_bytes(bytes(blob))


def _spawn_blob() -> bytes:
    low = (VMAP_COORD_OFFSET - 10.0, VMAP_COORD_OFFSET - 10.0, 0.0)
    high = (VMAP_COORD_OFFSET, VMAP_COORD_OFFSET, 10.0)
    blob = bytearray()
    blob += struct.pack("<I", 4)              # MOD_HAS_BOUND
    blob += struct.pack("<H", 0) + struct.pack("<I", 1)
    blob += struct.pack("<3f", VMAP_COORD_OFFSET - 5.0,
                        VMAP_COORD_OFFSET - 5.0, 0.0)
    blob += struct.pack("<3f", 0.0, 0.0, 0.0)
    blob += struct.pack("<f", 1.0)
    blob += struct.pack("<6f", *low, *high)
    name = b"Testwall.wmo"
    blob += struct.pack("<I", len(name)) + name
    return bytes(blob)


def _write_vmtree_global(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    blob = bytearray()
    blob += b"VMAP_4.1" + b"\x00" + b"NODE"
    blob += struct.pack("<6f", 0, 0, 0, 0, 0, 0)
    blob += struct.pack("<2I", 0, 0)          # empty BIH tree + objects
    blob += b"GOBJ"
    blob += _spawn_blob()
    path.write_bytes(bytes(blob))


def _detour(world_point):
    x, y, z = world_point
    return (y, z, x)


def _write_navmesh(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    verts = [
        (0.0, 0.0, 0.0), (10.0, 0.0, 0.0), (10.0, 10.0, 0.0),
        (0.0, 10.0, 0.0), (20.0, 0.0, 0.0), (20.0, 10.0, 0.0),
        (30.0, 0.0, 0.0), (30.0, 10.0, 0.0),
    ]
    polygons = [
        ([0, 1, 2, 3], [0, 2, 0, 0]),
        ([1, 4, 5, 2], [0, 3, 0, 1]),
        ([4, 6, 7, 5], [0, 0, 0, 2]),
    ]
    payload = bytearray()
    payload += struct.pack(
        "<15i", DT_MESH_MAGIC, 7, 0, 0, 0, 0,
        len(polygons), len(verts), 0, 0, 0, 0, 0, 0, len(polygons))
    payload += struct.pack("<3f", 1.6, 0.5, 0.4)
    payload += struct.pack("<3f", *_detour((0, 0, 0)))
    payload += struct.pack("<3f", *_detour((30, 10, 0)))
    payload += struct.pack("<f", 1.0)
    for world_point in verts:
        payload += struct.pack("<3f", *_detour(world_point))
    for verts_index, neighbours in polygons:
        padded_verts = list(verts_index) + [0] * (6 - len(verts_index))
        padded_neis = list(neighbours) + [0] * (6 - len(neighbours))
        payload += struct.pack("<I", 0)
        payload += struct.pack("<6H", *padded_verts)
        payload += struct.pack("<6H", *padded_neis)
        payload += struct.pack("<H2B", 0, len(verts_index), 1)
    header = struct.pack("<IIIIB3x", 0x4D4D4150, 7, 15, len(payload), 1)
    path.write_bytes(header + payload)


def test_terrain_height_and_holes(tmp_path: Path):
    path = tmp_path / "maps" / "0003232.map"
    _write_terrain(path, height=20.0)
    tile = load_terrain_tile(str(path), 32, 32)
    assert tile is not None
    assert tile.height_at(0.0, 0.0) == 20.0
    layer = TerrainMap(
        types.SimpleNamespace(map_path=lambda *a: str(path)), 0)
    assert layer.ground_height(0.0, 0.0) == 20.0
    assert layer.segment_clear((0, 0, 25), (10, 0, 25))
    assert not layer.segment_clear((0, 0, 15), (10, 0, 15))

    hole_path = tmp_path / "maps" / "0003333.map"
    _write_terrain(hole_path, height=20.0, legacy_holes=True)
    hole_tile = load_terrain_tile(str(hole_path), 33, 33)
    assert hole_tile is not None
    assert hole_tile.height_at(0.0, 0.0) is None


def test_vmap_tile_parsing_and_group_los(tmp_path: Path):
    _write_vmtile(tmp_path / "vmaps" / "000_31_31.vmtile")
    _write_vmo(tmp_path / "vmaps" / "Testwall.wmo.vmo")
    tile = load_vmap_tile(str(tmp_path / "vmaps" / "000_31_31.vmtile"),
                          31, 31)
    assert tile is not None and len(tile.spawns) == 1
    placement = tile.spawns[0]
    low, high = placement.world_bounds()
    assert abs(low[0]) < 0.01 and abs(low[1]) < 0.01
    assert abs(high[0] - 10.0) < 0.01 and abs(high[1] - 10.0) < 0.01

    layer = VMapMap(ClientData(str(tmp_path)), 0)
    # Local +y points towards world -y, so the doorway is around world y=0.
    assert layer.segment_clear((-5.0, 0.0, 5.0), (15.0, 0.0, 5.0))
    assert not layer.segment_clear((-5.0, 3.0, 5.0), (15.0, 3.0, 5.0))
    # A wall crossing is blocked even though the group AABB contains the ray.
    assert not layer.segment_clear((4.0, -2.0, 5.0), (6.0, 8.0, 5.0))
    assert layer.segment_clear((20.0, 20.0, 5.0), (30.0, 20.0, 5.0))
    # Two units on the same side of the wall can see each other.
    assert layer.segment_clear((2.0, 0.0, 5.0), (4.0, 0.0, 5.0))
    assert layer.segment_clear((15.0, 0.0, 5.0), (5.0, 0.0, 5.0))


def test_vmap_non_tiled_global_model(tmp_path: Path):
    _write_vmtree_global(tmp_path / "vmaps" / "000.vmtree")
    _write_vmo(tmp_path / "vmaps" / "Testwall.wmo.vmo")
    from src.world.vmap import load_vmap_tree_globals
    globals_ = load_vmap_tree_globals(str(tmp_path / "vmaps" / "000.vmtree"))
    assert len(globals_) == 1 and globals_[0].name == "Testwall.wmo"
    layer = VMapMap(ClientData(str(tmp_path)), 0)
    assert layer.segment_clear((-5.0, 0.0, 5.0), (15.0, 0.0, 5.0))
    assert not layer.segment_clear((-5.0, 3.0, 5.0), (15.0, 3.0, 5.0))


def test_navmesh_parse_and_pathfinding(tmp_path: Path):
    _write_navmesh(tmp_path / "mmaps" / "0003131.mmtile")
    layer = NavMeshMap(ClientData(str(tmp_path)), 0)
    assert grid_coord(5.0) == 31
    snapped = layer.point_on_mesh((5.0, 5.0, 0.0))
    assert snapped is not None
    assert abs(snapped[0] - 5.0) < 0.1 and abs(snapped[1] - 5.0) < 0.1

    path = layer.find_path((5.0, 5.0, 0.0), (25.0, 5.0, 0.0))
    assert path is not None and len(path) >= 2
    assert math.dist(path[-1], (25.0, 5.0, 0.0)) < 0.1
    # A direct line through all three polygons should be string-pulled.
    assert len(path) <= 4

    random_point = layer.random_point((15.0, 5.0, 0.0), 5.0)
    assert random_point is not None
    assert point_in_polygon((random_point[0], random_point[1]),
                            [(0.0, 0.0, 0.0), (30.0, 0.0, 0.0),
                             (30.0, 10.0, 0.0), (0.0, 10.0, 0.0)])


def test_world_geometry_combines_terrain_and_vmaps(tmp_path: Path):
    _write_vmtile(tmp_path / "vmaps" / "000_31_31.vmtile")
    _write_vmo(tmp_path / "vmaps" / "Testwall.wmo.vmo")
    # Placement is around world (0,0), which maps to tile 31,31.
    _write_terrain(tmp_path / "maps" / "0003131.map", height=20.0)
    world = geometry.WorldGeometry(ClientData(str(tmp_path)), None)
    assert world.terrain_ready and world.vmaps_ready
    assert world.ground_height(0, 5.0, 5.0) == 20.0
    # Through the doorway, then through the solid part of the wall.
    assert world.has_line_of_sight(0, (-5.0, 0.0, 5.0),
                                   (15.0, 0.0, 5.0))
    assert not world.has_line_of_sight(0, (-5.0, 3.0, 5.0),
                                       (15.0, 3.0, 5.0))
    # Above the wall and above the terrain.
    assert world.has_line_of_sight(0, (-5.0, 0.0, 30.0),
                                   (15.0, 0.0, 30.0))


@pytest.mark.skipif(
    not os.path.isfile(os.path.join(ROOT, "data", "client", "maps",
                                    "0004832.map")),
    reason="starter client-data cache is not populated")
def test_real_starter_data_smoke():
    world = geometry.WorldGeometry(
        ClientData(os.path.join(ROOT, "data", "client")), None)
    assert world.terrain_ready and world.vmaps_ready and world.navmesh_ready
    ground = world.ground_height(0, -8955.26, -132.19)
    assert ground is not None and abs(ground - 83.08) < 0.5
    assert world.has_line_of_sight(0, (-8955.26, -132.19, ground + 1.6),
                                   (-8940.0, -120.0, ground + 1.6))
    start = (-8955.26, -132.19, ground)
    end = (-8900.0, -120.0, world.ground_height(0, -8900.0, -120.0))
    path = world.find_path(0, start, end)
    assert path is not None and len(path) >= 2
    assert math.dist(path[-1], end) < 1.0


def test_monster_move_path_serializes_all_waypoints():
    payload = object_update.build_monster_move_path(
        0x1234, 1.0, 2.0, 3.0, 0.0,
        [(4.0, 5.0, 6.0), (7.0, 8.0, 9.0)],
        duration_ms=1500, move_id=7)
    # packed guid + toggle + start xyz + spline id + face mode + flags + time
    offset = len(pack_guid(0x1234)) + 1 + 12 + 4 + 1
    flags, duration, count = struct.unpack_from("<3I", payload, offset)
    assert (flags, duration, count) == (0, 1500, 2)
    points = struct.unpack_from("<6f", payload, offset + 12)
    assert points[:3] == (4.0, 5.0, 6.0)
    assert points[3:] == (7.0, 8.0, 9.0)
