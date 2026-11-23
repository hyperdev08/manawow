"""Reader for TrinityCore/CMaNGOS ``.map`` terrain files.

Only the data needed by the server is decoded: the heightmap and the per-chunk
holes.  Liquids are intentionally ignored here; the navigation mesh already
marks water polygons and the client remains authoritative about player
movement.

Two on-disk variants are supported:

* the current TrinityCore layout (``AREA`` + 16x16 ``uint16`` area ids,
  heights as ``float``/``int16``/``int8`` and 16x16x8 bytes of holes);
* the older CMaNGOS layout still used by most 3.3.5a extractions, where holes
  are 16x16 ``uint16`` quadrant masks (512 bytes).

All coordinates are ordinary WoW world coordinates.  The first tile is at the
north-west corner of the map, hence the ``32 - value / 533.333`` formula.
"""
from __future__ import annotations

import math
import os
import struct
from dataclasses import dataclass
from typing import Optional

from .client_data import (
    CENTER_GRID_ID,
    MAP_RESOLUTION,
    SIZE_OF_GRIDS,
    ClientData,
)

MAP_MAGIC = b"MAPS"
HEIGHT_MAGIC = b"MHGT"
INVALID_HEIGHT = -100000.0

# CMaNGOS hole masks: a uint16 per 8x8 chunk, each bit covers a 2x2 quad.
_HOLETAB_H = (0x1111, 0x2222, 0x4444, 0x8888)
_HOLETAB_V = (0x000F, 0x00F0, 0x0F00, 0xF000)

_HEIGHT_NO_HEIGHT = 0x0001
_HEIGHT_INT16 = 0x0002
_HEIGHT_INT8 = 0x0004


@dataclass
class TerrainTile:
    tile_x: int
    tile_y: int
    grid_height: float
    grid_max_height: float
    heights: Optional[list[float]]
    holes: Optional[bytes | tuple[int, ...]]
    holes_legacy: bool = False
    height_scale: float = 1.0

    # -- raw heightmap -------------------------------------------------
    def _height_raw(self, x: float, y: float) -> Optional[float]:
        if self.heights is None:
            return None
        local_x = MAP_RESOLUTION * (CENTER_GRID_ID - x / SIZE_OF_GRIDS)
        local_y = MAP_RESOLUTION * (CENTER_GRID_ID - y / SIZE_OF_GRIDS)
        cell_x = int(local_x)
        cell_y = int(local_y)
        frac_x = local_x - cell_x
        frac_y = local_y - cell_y
        cell_x &= MAP_RESOLUTION - 1
        cell_y &= MAP_RESOLUTION - 1
        if self._is_hole(cell_x, cell_y):
            return None

        # ``heights`` is V9 (129x129) followed by V8 (128x128).
        v9 = self.heights
        v8_offset = 129 * 129
        base = cell_x * 128 + cell_x + cell_y
        multiplier = self.height_scale

        h1 = v9[base]
        if frac_x + frac_y < 1.0:
            if frac_x > frac_y:
                h2 = v9[base + 129]
                h5 = 2.0 * v9[v8_offset + cell_x * 128 + cell_y]
                a, b, c = h2 - h1, h5 - h1 - h2, h1
            else:
                h3 = v9[base + 1]
                h5 = 2.0 * v9[v8_offset + cell_x * 128 + cell_y]
                a, b, c = h5 - h1 - h3, h3 - h1, h1
        else:
            if frac_x > frac_y:
                h2 = v9[base + 129]
                h4 = v9[base + 130]
                h5 = 2.0 * v9[v8_offset + cell_x * 128 + cell_y]
                a, b, c = h2 + h4 - h5, h4 - h2, h5 - h4
            else:
                h3 = v9[base + 1]
                h4 = v9[base + 130]
                h5 = 2.0 * v9[v8_offset + cell_x * 128 + cell_y]
                a, b, c = h4 - h3, h3 + h4 - h5, h5 - h4
        return (a * frac_x + b * frac_y + c) * multiplier + self.grid_height

    def _is_hole(self, row: int, col: int) -> bool:
        if not self.holes:
            return False
        cell_row = row // 8
        cell_col = col // 8
        if self.holes_legacy:
            hole = int(self.holes[cell_row * 16 + cell_col])
            hole_row = (row % 8) // 2
            hole_col = (col - cell_col * 8) // 2
            return bool(hole & _HOLETAB_H[hole_col] & _HOLETAB_V[hole_row])
        hole_row = row % 8
        hole_col = col % 8
        # ``holes`` here is a bytes object with the TC layout.
        return bool(self.holes[cell_row * 16 * 8 + cell_col * 8 + hole_row]
                    & (1 << hole_col))

    def height_at(self, x: float, y: float) -> Optional[float]:
        """Ground height at a world point, or ``None`` for a hole/no data."""
        value = self._height_raw(x, y)
        if value is None or value <= INVALID_HEIGHT / 2:
            return None
        return value


def _parse_holes(blob: bytes) -> tuple[Optional[bytes | tuple[int, ...]], bool]:
    if not blob:
        return None, False
    if len(blob) >= 16 * 16 * 8:
        # TrinityCore layout: one byte per 1x1 cell row, bit per column.
        return blob, False
    if len(blob) >= 16 * 16 * 2:
        # CMaNGOS layout: uint16 per 8x8 chunk, 2x2 quadrants per bit.
        values = struct.unpack_from("<%dH" % (16 * 16), blob, 0)
        return values, True
    return None, False


def load_terrain_tile(path: str, tile_x: int, tile_y: int
                      ) -> Optional[TerrainTile]:
    """Read a ``.map`` file into a :class:`TerrainTile`."""
    try:
        with open(path, "rb") as handle:
            data = handle.read()
    except OSError:
        return None
    if len(data) < 44 or data[:4] != MAP_MAGIC:
        return None
    try:
        (_area_off, _area_size, height_off, height_size,
         _liquid_off, _liquid_size, holes_off, holes_size) = struct.unpack_from(
            "<8I", data, 12)
    except struct.error:
        return None
    if not height_off or height_off + 16 > len(data):
        return None
    if data[height_off:height_off + 4] != HEIGHT_MAGIC:
        return None
    try:
        flags, grid_height, grid_max_height = struct.unpack_from(
            "<I2f", data, height_off + 4)
    except struct.error:
        return None

    heights: Optional[list[float]] = None
    height_scale = 1.0
    if not flags & _HEIGHT_NO_HEIGHT:
        cursor = height_off + 16
        try:
            if flags & _HEIGHT_INT16:
                count_v9 = 129 * 129
                count_v8 = 128 * 128
                need = (count_v9 + count_v8) * 2
                if cursor + need > len(data):
                    return None
                raw = struct.unpack_from("<%dH" % (count_v9 + count_v8),
                                         data, cursor)
                heights = [float(value) for value in raw]
                height_scale = (grid_max_height - grid_height) / 65535.0
            elif flags & _HEIGHT_INT8:
                count_v9 = 129 * 129
                count_v8 = 128 * 128
                need = count_v9 + count_v8
                if cursor + need > len(data):
                    return None
                heights = [float(value) for value in
                           struct.unpack_from("<%dB" % need, data, cursor)]
                height_scale = (grid_max_height - grid_height) / 255.0
            else:
                count = 129 * 129 + 128 * 128
                need = count * 4
                if cursor + need > len(data):
                    return None
                heights = list(struct.unpack_from("<%df" % count, data,
                                                  cursor))
                height_scale = 1.0
        except struct.error:
            return None

    holes = None
    legacy = False
    if holes_size and holes_off and holes_off + holes_size <= len(data):
        holes, legacy = _parse_holes(data[holes_off:holes_off + holes_size])

    return TerrainTile(tile_x, tile_y, float(grid_height),
                       float(grid_max_height), heights, holes, legacy,
                       height_scale)


class TerrainMap:
    """Lazy, tile-cached terrain lookup for one map id."""

    def __init__(self, data: ClientData, map_id: int, cache_limit: int = 64):
        self.data = data
        self.map_id = int(map_id)
        self.cache_limit = max(1, int(cache_limit))
        self._tiles: dict[tuple[int, int], Optional[TerrainTile]] = {}
        self._order: list[tuple[int, int]] = []

    def _tile(self, tile_x: int, tile_y: int) -> Optional[TerrainTile]:
        key = (int(tile_x), int(tile_y))
        if key in self._tiles:
            return self._tiles[key]
        tile = load_terrain_tile(
            self.data.map_path(self.map_id, key[0], key[1]), key[0], key[1])
        self._tiles[key] = tile
        self._order.append(key)
        while len(self._order) > self.cache_limit:
            old = self._order.pop(0)
            if old != key:
                self._tiles.pop(old, None)
        return tile

    def ground_height(self, x: float, y: float) -> Optional[float]:
        tile_x = int(CENTER_GRID_ID - x / SIZE_OF_GRIDS)
        tile_y = int(CENTER_GRID_ID - y / SIZE_OF_GRIDS)
        if not (0 <= tile_x <= 63 and 0 <= tile_y <= 63):
            return None
        tile = self._tile(tile_x, tile_y)
        if tile is None:
            return None
        return tile.height_at(float(x), float(y))

    def segment_clear(self, start: tuple[float, float, float],
                      end: tuple[float, float, float],
                      clearance: float = 0.05,
                      step: float = 0.5) -> bool:
        """True when a segment does not pass through terrain.

        Sampling is deliberately in world space: it catches hills and ridges
        even when both endpoints are in the air.  Missing map files mean "no
        terrain", which keeps the server usable before the data cache is
        populated.
        """
        dx = end[0] - start[0]
        dy = end[1] - start[1]
        horizontal = math.hypot(dx, dy)
        samples = max(2, min(512, int(horizontal / max(0.25, step)) + 1))
        # Fast reject when the whole segment is far above gentle terrain is
        # impossible without reading the tile, so just walk it.
        for index in range(samples):
            t = index / (samples - 1)
            x = start[0] + dx * t
            y = start[1] + dy * t
            z = start[2] + (end[2] - start[2]) * t
            ground = self.ground_height(x, y)
            if ground is None:
                continue
            if z + clearance < ground:
                return False
        return True


def terrain_available(data: Optional[ClientData]) -> bool:
    return bool(data and os.path.isdir(data.maps_dir)
                and any(name.endswith(".map")
                        for name in os.listdir(data.maps_dir)))
