"""Combined terrain, static VMap and navigation-mesh geometry.

This is the only module the gameplay code needs to talk to.  It resolves the
optional client-data directory from the server config, keeps one cache per
data directory, and degrades to the previous "open world" behaviour when the
cache is empty.

Configuration::

    worldserver:
      client_data: "./data/client"

Run ``python tools/fetch_client_data.py --preset starter`` to populate the
default cache.  The server never downloads data itself; geometry lookups just
return "unknown/clear" so a missing cache cannot stop the worldserver booting.
"""
from __future__ import annotations

import os
from typing import Optional

from . import los
from .client_data import ClientData, get_client_data, grid_coord
from .navmesh import NAV_GROUND_FLAGS, NavMeshMap, navmesh_available
from .terrain import TerrainMap, terrain_available
from .vmap import VMapMap, vmaps_available


def configured_data_dir(config) -> Optional[str]:
    """Read ``worldserver.client_data`` without assuming a real Config object."""
    server = getattr(config, "worldserver", None)
    path = getattr(server, "client_data", None) if server is not None else None
    if not path:
        path = os.environ.get("MANAWOW_CLIENT_DATA")
    return os.path.expanduser(str(path)) if path else None


class WorldGeometry:
    def __init__(self, data: ClientData, world_path: Optional[str] = None):
        self.data = data
        self.world_path = world_path
        self._terrain: dict[int, TerrainMap] = {}
        self._vmap: dict[int, VMapMap] = {}
        self._navmesh: dict[int, NavMeshMap] = {}
        self.terrain_ready = terrain_available(data)
        self.vmaps_ready = vmaps_available(data)
        self.navmesh_ready = navmesh_available(data)
        # Cached set of generated mmap tiles.  Checking whether a creature is
        # standing on a map/area with real navmesh data must not stat/parse a
        # tile on the event loop during every chase tick.
        self.navmesh_tiles: set[tuple[int, int, int]] = set()
        if self.navmesh_ready:
            try:
                for name in os.listdir(self.data.mmaps_dir):
                    if not name.endswith(".mmtile"):
                        continue
                    stem = name[:-7]
                    if len(stem) >= 7 and stem[:3].isdigit() \
                            and stem[3:5].isdigit() and stem[5:7].isdigit():
                        self.navmesh_tiles.add(
                            (int(stem[:3]), int(stem[3:5]), int(stem[5:7])))
            except OSError:
                pass

    # -- object caches -------------------------------------------------
    def terrain(self, map_id: int) -> TerrainMap:
        map_id = int(map_id)
        result = self._terrain.get(map_id)
        if result is None:
            result = TerrainMap(self.data, map_id)
            self._terrain[map_id] = result
        return result

    def vmap(self, map_id: int) -> VMapMap:
        map_id = int(map_id)
        result = self._vmap.get(map_id)
        if result is None:
            result = VMapMap(self.data, map_id)
            self._vmap[map_id] = result
        return result

    def navmesh(self, map_id: int) -> NavMeshMap:
        map_id = int(map_id)
        result = self._navmesh.get(map_id)
        if result is None:
            result = NavMeshMap(self.data, map_id)
            self._navmesh[map_id] = result
        return result

    # -- queries -------------------------------------------------------
    def has_line_of_sight(self, map_id: int, start, end) -> bool:
        start = (float(start[0]), float(start[1]), float(start[2]))
        end = (float(end[0]), float(end[1]), float(end[2]))
        if self.world_path:
            if not los.has_los(self.world_path, int(map_id), start, end):
                return False
        if self.vmaps_ready and not self.vmap(map_id).segment_clear(start, end):
            return False
        if self.terrain_ready and not self.terrain(map_id).segment_clear(
                start, end):
            return False
        return True

    def ground_height(self, map_id: int, x: float, y: float) -> Optional[float]:
        if self.terrain_ready:
            height = self.terrain(map_id).ground_height(x, y)
            if height is not None:
                return height
        if self.navmesh_ready:
            snapped = self.navmesh(map_id).point_on_mesh(
                (x, y, 0.0), max_distance=8.0, use_z=False,
                include_flags=NAV_GROUND_FLAGS)
            if snapped is not None:
                return snapped[2]
        return None

    def find_path(self, map_id: int, start, end, max_tiles: int = 2,
                  include_flags: int = NAV_GROUND_FLAGS):
        if not self.navmesh_ready:
            return None
        return self.navmesh(map_id).find_path(
            start, end, max_tiles=max_tiles, include_flags=include_flags)

    def point_on_mesh(self, map_id: int, point,
                      include_flags: int = NAV_GROUND_FLAGS):
        if not self.navmesh_ready:
            return None
        return self.navmesh(map_id).point_on_mesh(
            point, include_flags=include_flags)

    def has_navmesh_at(self, map_id: int, point) -> bool:
        """True when the map has a generated navmesh tile at ``point``."""
        if not self.navmesh_ready:
            return False
        return (int(map_id), grid_coord(float(point[0])),
                grid_coord(float(point[1]))) in self.navmesh_tiles

    def random_point(self, map_id: int, center, radius: float,
                     include_flags: int = NAV_GROUND_FLAGS):
        if not self.navmesh_ready:
            return None
        return self.navmesh(map_id).random_point(
            center, radius, include_flags=include_flags)


_geometry_cache: dict[tuple[str, Optional[str]], WorldGeometry] = {}


def get_geometry(config, world_path: Optional[str] = None
                 ) -> Optional[WorldGeometry]:
    """Return the shared geometry object for this config, or ``None``."""
    path = configured_data_dir(config)
    if not path:
        return None
    data = get_client_data(path)
    if data is None:
        return None
    key = (data.root, os.path.abspath(world_path) if world_path else None)
    geometry = _geometry_cache.get(key)
    if geometry is None:
        geometry = WorldGeometry(data, world_path)
        _geometry_cache[key] = geometry
    return geometry


def clear_cache() -> None:
    _geometry_cache.clear()
    los.clear_cache()
