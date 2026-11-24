"""Run navigation-mesh work on a dedicated thread.

Parsing a navmesh tile, rebuilding the polygon graph and scanning every polygon
for ``random_point``/``nearest_poly`` are CPU/IO heavy.  The old code did this
on the asyncio thread, and the stall watchdog caught individual tiles blocking
all sessions for seconds during the first login in an area.  A single worker
also serialises access to the lazily populated ``NavMeshMap`` caches, so two
creatures cannot rebuild the same map at the same time.
"""
from __future__ import annotations

import asyncio
import functools
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, TypeVar

_GEOMETRY_POOL = ThreadPoolExecutor(
    max_workers=1, thread_name_prefix="world-geometry")
T = TypeVar("T")


async def run_geometry(func: Callable[..., T], /, *args: Any,
                       **kwargs: Any) -> T:
    loop = asyncio.get_running_loop()
    call = functools.partial(func, *args, **kwargs)
    return await loop.run_in_executor(_GEOMETRY_POOL, call)


def shutdown(wait: bool = True) -> None:
    _GEOMETRY_POOL.shutdown(wait=wait)
