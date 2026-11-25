"""Support for the split world / character database pair.

The repository no longer ships ``data/world.db``: the world bank is generated
from a pinned CMaNGOS dump (:mod:`tools.import_wotlk_db`), while mutable
per-character state lives in ``data/characters.db``.  Server code keeps
receiving the *world* database path everywhere, which is what the configuration
stores.  When a character database is configured, :func:`connect_character`
routes character reads/writes there and attaches the world database so joins
against item/spell/player templates keep working.

With no character path configured the module is a no-op: every helper opens the
single combined database, which is what the tests and one-off tools build.
"""
from __future__ import annotations

import os
import sqlite3
from typing import Optional

# World-owned tables that character queries join against.  They are exposed as
# read-only temp views on character connections so callers can keep writing
# ``JOIN item_templates`` without knowing which file owns the table.
CHARACTER_WORLD_VIEWS = (
    "item_templates",
    "player_classlevelstats",
    "player_levelstats",
    "playercreateinfo_spell",
    "playercreateinfo_action",
    "spell_templates",
)

_world_path: Optional[str] = None
_character_path: Optional[str] = None


def _normalise(path: Optional[str]) -> Optional[str]:
    if not path:
        return None
    return os.path.normpath(os.path.abspath(os.fspath(path)))


def same_path(first: Optional[str], second: Optional[str]) -> bool:
    """Cross-platform, case-insensitive path comparison on Windows."""
    if not first or not second:
        return False
    return os.path.normcase(os.path.abspath(first)) == os.path.normcase(
        os.path.abspath(second))


def configure(world_path: Optional[str],
              character_path: Optional[str] = None) -> None:
    """Route future character-database calls to a separate file.

    ``character_path`` may be empty or equal to ``world_path``; both mean "use
    the single combined database" for backwards compatibility with tests and
    tools that pass one path around.
    """
    global _world_path, _character_path
    _world_path = _normalise(world_path)
    character = _normalise(character_path)
    if character is None or character == _world_path:
        _character_path = None
    else:
        _character_path = character


def reset() -> None:
    """Forget the configured paths (used by tests)."""
    global _world_path, _character_path
    _world_path = None
    _character_path = None


def is_split() -> bool:
    return _character_path is not None


def world_path(path: Optional[str] = None) -> str:
    """Return the world database path, preferring the configured one."""
    resolved = _world_path or _normalise(path)
    if resolved is None:
        raise ValueError("no world database path configured")
    return resolved


def character_path(path: Optional[str] = None) -> str:
    """Return the character database path (the caller's path when combined)."""
    if _character_path is not None:
        return _character_path
    resolved = _normalise(path) or _world_path
    if resolved is None:
        raise ValueError("no character database path configured")
    return resolved


def connect_world(path: Optional[str] = None) -> sqlite3.Connection:
    return sqlite3.connect(world_path(path))


def _install_world_views(con: sqlite3.Connection) -> None:
    existing = {
        row[0]
        for row in con.execute(
            "SELECT name FROM main.sqlite_master WHERE type = 'table'")
    }
    for name in CHARACTER_WORLD_VIEWS:
        if name in existing:
            continue
        try:
            con.execute(
                f'CREATE TEMP VIEW IF NOT EXISTS "{name}" AS'
                f' SELECT * FROM world."{name}"')
        except sqlite3.Error:
            # A world database generated before a table existed (or a
            # schema-only test database) simply lacks that view.
            pass


def connect_character(path: Optional[str] = None) -> sqlite3.Connection:
    """Open the database that owns characters, inventory and quest logs.

    In split mode the world database is attached under the ``world`` schema and
    the template tables are exposed as temp views.  Otherwise this is a plain
    connection to the combined database at ``path``.
    """
    if _character_path is None:
        return sqlite3.connect(character_path(path))
    if path is not None and not (same_path(path, _world_path)
                                 or same_path(path, _character_path)):
        # A caller passed an unrelated database (a test/tool building a
        # self-contained file): do not hijack it into the configured store.
        return sqlite3.connect(path)
    con = sqlite3.connect(_character_path)
    con.execute("ATTACH DATABASE ? AS world", (_world_path,))
    _install_world_views(con)
    return con
