#!/usr/bin/env python3
"""Download extracted 3.3.5a terrain, VMap and MMap data.

The worldserver itself never downloads anything; this tool fills
``data/client/{maps,vmaps,mmaps}`` from a mirror of the client extracts.  The
default source is the public ``Torrer/TrinityCore-3.3.5-data`` GitHub mirror
(``VMAP_4.1`` / map format v1.3), which matches the readers in
``src/world/terrain.py``, ``src/world/vmap.py`` and ``src/world/navmesh.py``.

Examples::

    # Northshire / Elwynn starter area (map 0, tile 48,32 +/- 2 tiles)
    python tools/fetch_client_data.py --preset starter

    # A different map and tile
    python tools/fetch_client_data.py --map 1 --tile 42,25

    # Around a world position
    python tools/fetch_client_data.py --map 0 --around -8955,-132 --radius 3

    # Everything the mirror has for a map (large!)
    python tools/fetch_client_data.py --map 0 --all-map

Only static WMO geometry (``.wmo.vmo``) is downloaded for selected tiles.  M2
models use their placement bounds for line of sight, so their per-model files
are optional and skipped by default.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, root)

from src.world.client_data import (  # noqa: E402
    grid_coord,
    map_file_name,
    mmap_file_name,
    vmap_tile_name,
    vmap_tree_name,
)
from src.world.vmap import load_vmap_tile, load_vmap_tree_globals  # noqa: E402

DEFAULT_SOURCE = (
    "https://raw.githubusercontent.com/Torrer/TrinityCore-3.3.5-data/master"
)
STARTER_CENTER = (-8955.26, -132.19)
STARTER_MAP = 0
STARTER_RADIUS = 2


def log(message: str) -> None:
    print(message, flush=True)


def _validate(path: str, rel: str) -> bool:
    try:
        with open(path, "rb") as handle:
            head = handle.read(16)
    except OSError:
        return False
    if not head:
        return False
    if rel.endswith(".map"):
        return head[:4] == b"MAPS"
    if rel.endswith(".mmtile"):
        # The on-disk magic is the little-endian uint32 0x4d4d4150.
        return head[:4] == b"PAMM"
    if rel.endswith((".vmtile", ".vmtree", ".vmo")):
        return head.startswith(b"VMAP_")
    return True


def download(source: str, rel: str, data_dir: str, force: bool = False,
             quiet: bool = False) -> bool:
    """Download one repository-relative file, returning True when present."""
    destination = os.path.join(data_dir, *rel.split("/"))
    if not force and os.path.isfile(destination) and _validate(destination,
                                                               rel):
        return True
    os.makedirs(os.path.dirname(destination), exist_ok=True)
    url = source.rstrip("/") + "/" + urllib.parse.quote(rel)
    request = urllib.request.Request(url, headers={"User-Agent": "ManaWoW-data-fetch"})
    last_error = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=90) as response:
                payload = response.read()
            if not payload:
                raise OSError("empty response")
            temp = destination + ".part"
            with open(temp, "wb") as handle:
                handle.write(payload)
            if not _validate(temp, rel):
                os.remove(temp)
                raise OSError("unexpected file format")
            os.replace(temp, destination)
            if not quiet:
                log(f"  {rel} ({len(payload):,} bytes)")
            return True
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                if not quiet:
                    log(f"  {rel}: not in source (404)")
                return False
            last_error = exc
        except (OSError, urllib.error.URLError) as exc:
            last_error = exc
        time.sleep(0.5 * (attempt + 1))
    if not quiet:
        log(f"  {rel}: failed ({last_error})")
    return False


def github_tree(source: str) -> list[str]:
    """List all files in a raw.githubusercontent.com mirror."""
    parsed = urllib.parse.urlparse(source)
    if parsed.netloc != "raw.githubusercontent.com":
        raise SystemExit(
            "--all-map needs a raw.githubusercontent.com source so the file "
            "tree can be listed with the GitHub API")
    pieces = [p for p in parsed.path.split("/") if p]
    if len(pieces) < 3:
        raise SystemExit(f"cannot parse source URL: {source}")
    owner, repository, branch = pieces[0], pieces[1], pieces[2]
    api = (f"https://api.github.com/repos/{owner}/{repository}"
           f"/git/trees/{branch}?recursive=1")
    request = urllib.request.Request(api, headers={
        "User-Agent": "ManaWoW-data-fetch",
        "Accept": "application/vnd.github+json",
    })
    with urllib.request.urlopen(request, timeout=120) as response:
        payload = json.load(response)
    if payload.get("truncated"):
        log("warning: GitHub tree listing was truncated; use --tile for "
            "the missing files")
    return [entry["path"] for entry in payload.get("tree", [])
            if entry.get("type") == "blob"]


def tiles_around(x: float, y: float, radius: int) -> list[tuple[int, int]]:
    center_x = grid_coord(x)
    center_y = grid_coord(y)
    result = []
    for dy in range(-radius, radius + 1):
        for dx in range(-radius, radius + 1):
            tile_x, tile_y = center_x + dx, center_y + dy
            if 0 <= tile_x <= 63 and 0 <= tile_y <= 63:
                result.append((tile_x, tile_y))
    return result


def fetch_tile_set(source: str, data_dir: str, map_id: int,
                   tiles: list[tuple[int, int]], force: bool = False,
                   with_m2: bool = False) -> None:
    log(f"Map {map_id}: {len(tiles)} tile(s)")
    log("  terrain")
    for tile_x, tile_y in tiles:
        download(source, f"maps/{map_file_name(map_id, tile_x, tile_y)}",
                 data_dir, force=force, quiet=True)
    log("  navigation meshes")
    for tile_x, tile_y in tiles:
        download(source, f"mmaps/{mmap_file_name(map_id, tile_x, tile_y)}",
                 data_dir, force=force, quiet=True)
    log("  world-model placements")
    models: set[str] = set()
    for tile_x, tile_y in tiles:
        rel = f"vmaps/{vmap_tile_name(map_id, tile_x, tile_y)}"
        if not download(source, rel, data_dir, force=force, quiet=True):
            continue
        path = os.path.join(data_dir, "vmaps",
                            vmap_tile_name(map_id, tile_x, tile_y))
        parsed = load_vmap_tile(path, tile_x, tile_y)
        if parsed is None:
            continue
        for spawn in parsed.spawns:
            lower = spawn.name.lower()
            if lower.endswith(".wmo") or (with_m2 and lower.endswith(".m2")):
                models.add(spawn.name)
    # Non-tiled maps store their single world model in the tree instead of a
    # .vmtile, so parse it for model references as well.
    tree_name = vmap_tree_name(map_id)
    if download(source, f"vmaps/{tree_name}", data_dir, force=force,
                quiet=True):
        tree_path = os.path.join(data_dir, "vmaps", tree_name)
        for spawn in load_vmap_tree_globals(tree_path):
            lower = spawn.name.lower()
            if lower.endswith(".wmo") or (with_m2 and lower.endswith(".m2")):
                models.add(spawn.name)
    if models:
        log(f"  model collision ({len(models)} file(s))")
        for model in sorted(models):
            download(source, f"vmaps/{model}.vmo", data_dir, force=force,
                     quiet=True)


def fetch_entire_map(source: str, data_dir: str, map_id: int,
                     force: bool = False) -> None:
    log(f"Listing source tree for map {map_id} ...")
    files = github_tree(source)
    prefix_map = f"maps/{map_id:03d}"
    prefix_mmap = f"mmaps/{map_id:03d}"
    prefix_vmap = f"vmaps/{map_id:03d}"
    selected = [name for name in files
                if name.startswith((prefix_map, prefix_mmap, prefix_vmap))]
    if not selected:
        log(f"  no files found for map {map_id}")
        return
    log(f"  {len(selected)} file(s), this can take a while")
    for rel in selected:
        download(source, rel, data_dir, force=force, quiet=True)
        if rel.endswith(".vmtile"):
            path = os.path.join(data_dir, *rel.split("/"))
            try:
                tile_x = int(os.path.basename(rel).split("_")[2].split(".")[0])
                tile_y = int(os.path.basename(rel).split("_")[1])
            except (IndexError, ValueError):
                continue
            parsed = load_vmap_tile(path, tile_x, tile_y)
            if parsed is None:
                continue
            for spawn in parsed.spawns:
                if spawn.name.lower().endswith(".wmo"):
                    download(source, f"vmaps/{spawn.name}.vmo", data_dir,
                             force=force, quiet=True)
    tree_name = vmap_tree_name(map_id)
    tree_path = os.path.join(data_dir, "vmaps", tree_name)
    if os.path.isfile(tree_path):
        for spawn in load_vmap_tree_globals(tree_path):
            if spawn.name.lower().endswith(".wmo"):
                download(source, f"vmaps/{spawn.name}.vmo", data_dir,
                         force=force, quiet=True)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", default=os.path.join(root, "data",
                                                           "client"))
    parser.add_argument("--source", default=DEFAULT_SOURCE)
    parser.add_argument("--map", type=int, action="append",
                        help="map id (repeatable; default 0)")
    parser.add_argument("--tile", action="append", default=[],
                        metavar="X,Y", help="explicit map tile")
    parser.add_argument("--around", metavar="X,Y",
                        help="select tiles around a world position")
    parser.add_argument("--radius", type=int, default=2,
                        help="tile radius for --around/--preset (default 2)")
    parser.add_argument("--preset", choices=["starter"], default=None,
                        help="starter = Northshire/Elwynn on map 0")
    parser.add_argument("--all-map", action="store_true",
                        help="download every file the mirror has for the map")
    parser.add_argument("--with-m2", action="store_true",
                        help="also download M2 .vmo files (not needed for LOS)")
    parser.add_argument("--force", action="store_true",
                        help="re-download files that already exist")
    args = parser.parse_args(argv)

    os.makedirs(args.data_dir, exist_ok=True)
    log(f"Data directory: {os.path.abspath(args.data_dir)}")
    log(f"Source: {args.source}")

    explicit_tiles = []
    for value in args.tile:
        try:
            tile_x, tile_y = (int(part) for part in value.split(",", 1))
        except ValueError:
            parser.error(f"invalid --tile {value!r}; expected X,Y")
        explicit_tiles.append((tile_x, tile_y))

    maps = args.map or [STARTER_MAP]
    for map_id in maps:
        if args.all_map:
            fetch_entire_map(args.source, args.data_dir, map_id,
                             force=args.force)
            continue
        tiles = list(explicit_tiles)
        if args.preset == "starter":
            tiles.extend(tiles_around(STARTER_CENTER[0], STARTER_CENTER[1],
                                      args.radius))
        if args.around:
            try:
                x, y = (float(part) for part in args.around.split(",", 1))
            except ValueError:
                parser.error("--around expects X,Y")
            tiles.extend(tiles_around(x, y, args.radius))
        if not tiles:
            tiles = tiles_around(STARTER_CENTER[0], STARTER_CENTER[1],
                                 args.radius)
        # De-duplicate while preserving order.
        seen = set()
        unique = []
        for tile in tiles:
            if tile not in seen:
                seen.add(tile)
                unique.append(tile)
        fetch_tile_set(args.source, args.data_dir, map_id, unique,
                       force=args.force, with_m2=args.with_m2)

    log("Done.")
    log("The server reads worldserver.client_data from config/config.yaml "
        f"(default {os.path.relpath(args.data_dir, root)!r}).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
