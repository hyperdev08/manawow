"""Line-of-sight checks with optional static blockers.

The reference server uses VMAP/MMAP data extracted from the client's map
files.  The full extract is not committed (it is several gigabytes), so the
runtime reads it from ``worldserver.client_data`` when available (see
``src/world/geometry.py``).  This module is the lightweight fallback and the
hand-authored overlay for special cases; it reads an optional SQLite table::

    CREATE TABLE sight_blockers(
        map INTEGER NOT NULL,
        min_x REAL, max_x REAL,
        min_y REAL, max_y REAL,
        min_z REAL, max_z REAL
    );

A segment is blocked when it intersects any axis-aligned box on the same map.
With no rows the world is treated as open, which is the correct fallback for
the current data set.  Adding extracted VMAP boxes, or a small hand-authored
set for a dungeon, immediately activates real LOS without touching the combat
code.
"""
from __future__ import annotations

import math
import os
import sqlite3
from dataclasses import dataclass
from typing import Iterable, Sequence

Point = tuple[float, float, float]


@dataclass(frozen=True)
class SightBlocker:
    map_id: int
    min_x: float
    max_x: float
    min_y: float
    max_y: float
    min_z: float
    max_z: float


def _point_in_box(point: Sequence[float], box: SightBlocker,
                  epsilon: float = 1e-6) -> bool:
    x, y, z = point
    return (box.min_x - epsilon <= x <= box.max_x + epsilon
            and box.min_y - epsilon <= y <= box.max_y + epsilon
            and box.min_z - epsilon <= z <= box.max_z + epsilon)


def segment_intersects_box(start: Sequence[float], end: Sequence[float],
                           box: SightBlocker, epsilon: float = 1e-6) -> bool:
    """Slab test for a 3D segment against an axis-aligned box.

    A segment that starts or ends inside the box is *not* considered blocked;
    that is what lets a unit standing in a doorway or on a bridge still see out
    instead of being permanently blinded by its own collision volume.
    """
    if _point_in_box(start, box, epsilon) or _point_in_box(end, box, epsilon):
        return False
    t_min = 0.0
    t_max = 1.0
    for origin, destination, low, high in (
        (start[0], end[0], box.min_x, box.max_x),
        (start[1], end[1], box.min_y, box.max_y),
        (start[2], end[2], box.min_z, box.max_z),
    ):
        direction = destination - origin
        if abs(direction) < epsilon:
            if origin < low or origin > high:
                return False
            continue
        inv = 1.0 / direction
        t1 = (low - origin) * inv
        t2 = (high - origin) * inv
        if t1 > t2:
            t1, t2 = t2, t1
        t_min = max(t_min, t1)
        t_max = min(t_max, t2)
        if t_min > t_max:
            return False
    return t_max >= 0.0 and t_min <= 1.0


def has_line_of_sight(start: Sequence[float], end: Sequence[float],
                      blockers: Iterable[SightBlocker],
                      map_id: int = 0) -> bool:
    for box in blockers:
        if int(box.map_id) != int(map_id):
            continue
        if segment_intersects_box(start, end, box):
            return False
    return True


_cache: dict[str, list[SightBlocker]] = {}


def load_blockers(path: str) -> list[SightBlocker]:
    """Load and cache blockers from ``sight_blockers`` (empty when absent).

    The world database is regenerated offline (the docs require stopping the
    server first), so the blocker set is process-static.  Earlier versions
    statted the database on every line-of-sight query; on Windows that
    filesystem metadata call can block for hundreds of milliseconds during
    combat, freezing the whole event loop.  Load once and keep it cached.
    """
    if not path:
        return []
    cached = _cache.get(path)
    if cached is not None:
        return cached
    # Only stat on the first load: the result is cached for the process, so the
    # Windows metadata call cannot stall the event loop on every LOS query.
    if not os.path.exists(path):
        _cache[path] = []
        return []
    try:
        con = sqlite3.connect(path)
        try:
            rows = con.execute(
                "SELECT map, min_x, max_x, min_y, max_y, min_z, max_z"
                " FROM sight_blockers"
            ).fetchall()
        finally:
            con.close()
    except sqlite3.Error:
        rows = []
    blockers = [
        SightBlocker(int(row[0]), float(row[1]), float(row[2]),
                     float(row[3]), float(row[4]), float(row[5]),
                     float(row[6]))
        for row in rows
    ]
    _cache[path] = blockers
    return blockers


def has_los(path: str, map_id: int, start: Sequence[float],
            end: Sequence[float]) -> bool:
    blockers = load_blockers(path)
    if not blockers:
        return True
    return has_line_of_sight(start, end, blockers, map_id=map_id)


def clear_cache() -> None:
    _cache.clear()
