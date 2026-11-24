#!/usr/bin/env python3
"""Create/upgrade the SQLite auth and world databases."""
from __future__ import annotations

import os
import sqlite3
import sys

root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(root, "src"))
sys.path.insert(0, root)

from src.common.config import Config
from src.world.character_db import init_character_db
from src.world.creature import init_creature_db
from src.world.quests import init_quest_db
from src.world.gameobject import init_gameobject_db


SQL_AUTH = """
CREATE TABLE IF NOT EXISTS account(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  username TEXT UNIQUE NOT NULL,
  sha_pass_hash TEXT,
  v BLOB,
  s BLOB,
  session_key BLOB,
  email TEXT,
  joindate TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  last_ip TEXT DEFAULT '127.0.0.1',
  failed_logins INTEGER DEFAULT 0,
  locked TINYINT DEFAULT 0,
  last_login TIMESTAMP,
  online INTEGER DEFAULT 0,
  expansion INTEGER DEFAULT 2,
  mutetime INTEGER DEFAULT 0,
  locale INTEGER DEFAULT 0,
  recruiter INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS realm(
  id INTEGER PRIMARY KEY,
  name TEXT,
  address TEXT,
  port INTEGER,
  icon INTEGER,
  color INTEGER,
  timezone INTEGER,
  allowed_security_level INTEGER DEFAULT 0,
  population REAL DEFAULT 0,
  gamebuild INTEGER DEFAULT 12340
);
"""

# Columns that older versions of this experimental project may be missing.
ACCOUNT_COLUMNS = {
    "sha_pass_hash": "TEXT",
    "v": "BLOB",
    "s": "BLOB",
    "session_key": "BLOB",
    "email": "TEXT",
    "joindate": "TIMESTAMP DEFAULT CURRENT_TIMESTAMP",
    "last_ip": "TEXT DEFAULT '127.0.0.1'",
    "failed_logins": "INTEGER DEFAULT 0",
    "locked": "TINYINT DEFAULT 0",
    "last_login": "TIMESTAMP",
    "online": "INTEGER DEFAULT 0",
    "expansion": "INTEGER DEFAULT 2",
    "mutetime": "INTEGER DEFAULT 0",
    "locale": "INTEGER DEFAULT 0",
    "recruiter": "INTEGER DEFAULT 0",
}


def _ensure_columns(con: sqlite3.Connection, table: str, columns: dict[str, str]) -> None:
    existing = {row[1] for row in con.execute(f"PRAGMA table_info({table})")}
    for name, definition in columns.items():
        if name not in existing:
            con.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


def init_auth_schema(con: sqlite3.Connection) -> None:
    con.executescript(SQL_AUTH)
    _ensure_columns(con, "account", ACCOUNT_COLUMNS)


def init_databases(cfg: Config) -> None:
    for path in (cfg.database.auth_path, cfg.database.world_path):
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)

    con = sqlite3.connect(cfg.database.auth_path)
    try:
        init_auth_schema(con)
        con.execute(
            "INSERT INTO realm(id, name, address, port, icon, color, timezone, population, gamebuild) "
            "VALUES(?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET name=excluded.name, address=excluded.address, "
            "port=excluded.port, timezone=excluded.timezone, population=excluded.population, "
            "gamebuild=excluded.gamebuild",
            (
                cfg.authserver.realm_id,
                cfg.authserver.realm_name,
                cfg.authserver.realm_address,
                cfg.worldserver.port,
                0,
                0,
                cfg.authserver.realm_timezone,
                cfg.authserver.realm_population,
                cfg.authserver.realm_build_max,
            ),
        )
        con.commit()
    finally:
        con.close()

    init_creature_db(cfg.database.world_path)
    init_character_db(cfg.database.world_path)
    init_quest_db(cfg.database.world_path)
    init_gameobject_db(cfg.database.world_path)


def load_config() -> Config:
    cfg_path = os.path.join(root, "config", "config.yaml")
    if not os.path.exists(cfg_path):
        print("config.yaml missing; using defaults from config.example.yaml")
        cfg_path = os.path.join(root, "config", "config.example.yaml")
    return Config.load(cfg_path)


def main() -> None:
    cfg = load_config()
    cfg.resolve_database_paths(root)
    init_databases(cfg)
    print(f"Initialized auth database:  {cfg.database.auth_path}")
    print(f"Initialized world database: {cfg.database.world_path}")


if __name__ == "__main__":
    main()
