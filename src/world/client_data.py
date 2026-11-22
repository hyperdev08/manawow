"""Locations and coordinate helpers for extracted 3.3.5a client data.

The world database contains spawns but no collision geometry.  The client
map/vmap/mmap extracts use three different but related layouts, so this module
keeps the naming and coordinate rules in one place:

``maps/``
    ``003xxyy.map`` -- heightmap, holes and liquids.  ``x``/``y`` are the
    normal WoW tile coordinates (0 at the north-west corner).

``mmaps/``
    ``003xxyy.mmtile`` -- Recast/Detour navigation mesh, same tile numbering.
    Vertices are stored Detour-style (x=world y, y=world z, z=world x).

``vmaps/``
    ``003.vmtree`` and ``003_yy_xx.vmtile`` (note the swapped tile order in
    the file name) plus one ``<model>.vmo`` per referenced world model.  Model
    placement coordinates are stored in the extractor's shifted space where a
    world point ``(x, y, z)`` becomes ``(17066.666 - x, 17066.666 - y, z)``.

Nothing here downloads data.  ``tools/fetch_client_data.py`` fills a cache
directory; when it is empty every consumer falls back to "no geometry".
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

# WoW's grid constants.  533.33333... yards per tile, 32 tiles from the map
# centre to either edge (the absolute coordinate limit is 17066.666...).
SIZE_OF_GRIDS = 533.3333333333333
MAP_HALF_SIZE = SIZE_OF_GRIDS * 32.0
CENTER_GRID_ID = 32
MAP_RESOLUTION = 128

# VMap model placements use the extractor's reflected/offset coordinate space.
VMAP_COORD_OFFSET = MAP_HALF_SIZE


def grid_coord(value: float) -> int:
    """Return the 0..63 tile index containing a world coordinate."""
    return int(CENTER_GRID_ID - value / SIZE_OF_GRIDS)


def grid_bounds(tile_x: int, tile_y: int) -> tuple[float, float, float, float]:
    """World-space ``(min_x, max_x, min_y, max_y)`` covered by a tile."""
    max_x = (CENTER_GRID_ID - tile_x) * SIZE_OF_GRIDS
    min_x = max_x - SIZE_OF_GRIDS
    max_y = (CENTER_GRID_ID - tile_y) * SIZE_OF_GRIDS
    min_y = max_y - SIZE_OF_GRIDS
    return min_x, max_x, min_y, max_y


def map_to_vmap_coords(point: tuple[float, float, float]
                       ) -> tuple[float, float, float]:
    """Convert world coordinates to the stored VMap model space."""
    return (VMAP_COORD_OFFSET - point[0],
            VMAP_COORD_OFFSET - point[1],
            point[2])


def vmap_to_map_coords(point: tuple[float, float, float]
                       ) -> tuple[float, float, float]:
    """Convert stored VMap model coordinates back to world space."""
    return (VMAP_COORD_OFFSET - point[0],
            VMAP_COORD_OFFSET - point[1],
            point[2])


def map_file_name(map_id: int, tile_x: int, tile_y: int) -> str:
    return f"{int(map_id):03d}{int(tile_x):02d}{int(tile_y):02d}.map"


def mmap_file_name(map_id: int, tile_x: int, tile_y: int) -> str:
    return f"{int(map_id):03d}{int(tile_x):02d}{int(tile_y):02d}.mmtile"


def vmap_tile_name(map_id: int, tile_x: int, tile_y: int) -> str:
    # TrinityCore's StaticMapTree::getTileFileName writes tileY first.
    return f"{int(map_id):03d}_{int(tile_y):02d}_{int(tile_x):02d}.vmtile"


def vmap_tree_name(map_id: int) -> str:
    return f"{int(map_id):03d}.vmtree"


def vmap_model_name(model: str) -> str:
    return f"{model}.vmo"


@dataclass(frozen=True)
class ClientData:
    """A directory containing ``maps/``, ``vmaps/`` and ``mmaps/``."""

    root: str

    @property
    def maps_dir(self) -> str:
        return os.path.join(self.root, "maps")

    @property
    def vmaps_dir(self) -> str:
        return os.path.join(self.root, "vmaps")

    @property
    def mmaps_dir(self) -> str:
        return os.path.join(self.root, "mmaps")

    def map_path(self, map_id: int, tile_x: int, tile_y: int) -> str:
        return os.path.join(self.maps_dir,
                            map_file_name(map_id, tile_x, tile_y))

    def mmap_path(self, map_id: int, tile_x: int, tile_y: int) -> str:
        return os.path.join(self.mmaps_dir,
                            mmap_file_name(map_id, tile_x, tile_y))

    def vmap_tree_path(self, map_id: int) -> str:
        return os.path.join(self.vmaps_dir, vmap_tree_name(map_id))

    def vmap_tile_path(self, map_id: int, tile_x: int, tile_y: int) -> str:
        return os.path.join(self.vmaps_dir,
                            vmap_tile_name(map_id, tile_x, tile_y))

    def vmap_model_path(self, model: str) -> str:
        # Model names come from the vmtile and are simple file names, but keep
        # the join safe for the odd name containing a slash.
        clean = str(model).replace("\\", "/").lstrip("/")
        return os.path.join(self.vmaps_dir, vmap_model_name(clean))

    def has_map(self, map_id: int, tile_x: int, tile_y: int) -> bool:
        return os.path.isfile(self.map_path(map_id, tile_x, tile_y))

    def has_mmap(self, map_id: int, tile_x: int, tile_y: int) -> bool:
        return os.path.isfile(self.mmap_path(map_id, tile_x, tile_y))

    def has_vmap_tile(self, map_id: int, tile_x: int, tile_y: int) -> bool:
        return os.path.isfile(self.vmap_tile_path(map_id, tile_x, tile_y))

    def has_any(self) -> bool:
        return any(os.path.isdir(path) and os.listdir(path)
                   for path in (self.maps_dir, self.vmaps_dir, self.mmaps_dir)
                   if os.path.isdir(path))


_cache: dict[str, ClientData] = {}


def get_client_data(path: Optional[str]) -> Optional[ClientData]:
    """Return a cached :class:`ClientData` for ``path`` (or ``None``)."""
    if not path:
        return None
    try:
        key = os.path.abspath(os.path.expanduser(str(path)))
    except (TypeError, ValueError):
        return None
    data = _cache.get(key)
    if data is None:
        data = ClientData(key)
        _cache[key] = data
    return data


def clear_cache() -> None:
    _cache.clear()
