"""Config loader."""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Dict

import yaml


@dataclass
class AuthConfig:
    host: str = "127.0.0.1"
    port: int = 3724
    realm_id: int = 1
    realm_name: str = "ManaWoW-Local"
    realm_address: str = "127.0.0.1"
    realm_type: int = 0
    realm_flags: int = 0
    realm_population: float = 0.0
    realm_timezone: int = 1
    realm_build_min: int = 12340
    realm_build_max: int = 12340


@dataclass
class WorldConfig:
    host: str = "127.0.0.1"
    port: int = 8085
    realm_id: int = 1
    motd: str = "ManaWoW"
    # Optional extracted client collision data (maps/, vmaps/, mmaps/).
    # An empty value disables terrain/LOS/navmesh; run
    # tools/fetch_client_data.py to populate the default directory.
    client_data: str = "./data/client"
    # Seconds a group may stay fully offline before it is disbanded.
    # 3.3.5a keeps the party available for a short reconnect window.
    # 0 disables the offline expiry.
    group_offline_expiry: int = 300
    # Copper granted to new characters (CMaNGOS StartPlayerMoney, default 0).
    start_money: int = 0


@dataclass
class DBConfig:
    auth_path: str = "./data/auth.db"
    world_path: str = "./data/world.db"
    # Empty means "characters live in world_path" (the combined layout used by
    # tests and one-off tools).  Config.load() fills in a sibling
    # characters.db for normal server runs.
    character_path: str = ""


@dataclass
class LogConfig:
    level: str = "INFO"
    log_dir: str = "./logs"
    console: bool = True
    # Write logs/packets_*.log.  Buffered and flushed in the background; set
    # false to disable packet dumps entirely.
    packet_log: bool = True


@dataclass
class Config:
    authserver: AuthConfig
    worldserver: WorldConfig
    database: DBConfig
    logging: LogConfig

    def resolve_database_paths(self, base_dir: str) -> None:
        """Make relative DB paths independent of the caller's CWD."""
        for attribute in ("auth_path", "world_path", "character_path"):
            value = getattr(self.database, attribute)
            if value and not os.path.isabs(value):
                setattr(self.database, attribute,
                        os.path.normpath(os.path.join(base_dir, value)))

    @classmethod
    def load(cls, path: str | os.PathLike[str]) -> "Config":
        with open(path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}

        def get(d: Dict[str, Any], *keys: str, default: Any = None):
            cur = d
            for k in keys:
                if not isinstance(cur, dict) or k not in cur:
                    return default
                cur = cur[k]
            return cur

        auth = AuthConfig(
            host=get(raw, "authserver", "host", default="127.0.0.1"),
            port=int(get(raw, "authserver", "port", default=3724)),
            realm_id=int(get(raw, "authserver", "realm_id", default=1)),
            realm_name=str(get(raw, "authserver", "realm_name", default="ManaWoW-Local")),
            realm_address=str(get(raw, "authserver", "realm_address", default="127.0.0.1")),
            realm_type=int(get(raw, "authserver", "realm_type", default=0)),
            realm_flags=int(get(raw, "authserver", "realm_flags", default=0)),
            realm_population=float(get(raw, "authserver", "realm_population", default=0.0)),
            realm_timezone=int(get(raw, "authserver", "realm_timezone", default=1)),
            realm_build_min=int(get(raw, "authserver", "realm_build_min", default=12340)),
            realm_build_max=int(get(raw, "authserver", "realm_build_max", default=12340)),
        )
        raw_client_data = get(raw, "worldserver", "client_data",
                              default="./data/client")
        world = WorldConfig(
            host=get(raw, "worldserver", "host", default="127.0.0.1"),
            port=int(get(raw, "worldserver", "port", default=8085)),
            realm_id=int(get(raw, "worldserver", "realm_id", default=1)),
            motd=str(get(raw, "worldserver", "motd", default="ManaWoW")),
            client_data=("" if raw_client_data is None
                         else str(raw_client_data)),
            group_offline_expiry=int(get(
                raw, "worldserver", "group_offline_expiry", default=300)),
        )
        world_path = str(get(raw, "database", "world_path",
                             default="./data/world.db"))
        raw_character = get(raw, "database", "character_path", default=None)
        if raw_character is None:
            # Default to a sibling characters.db so an existing config.yaml
            # (written before the split) automatically stops sharing one file.
            raw_character = os.path.join(
                os.path.dirname(world_path) or ".", "characters.db")
        db = DBConfig(
            auth_path=str(get(raw, "database", "auth_path", default="./data/auth.db")),
            world_path=world_path,
            character_path=str(raw_character or ""),
        )
        log = LogConfig(
            level=str(get(raw, "logging", "level", default="INFO")),
            log_dir=str(get(raw, "logging", "log_dir", default="./logs")),
            console=bool(get(raw, "logging", "console", default=True)),
            packet_log=bool(get(raw, "logging", "packet_log", default=True)),
        )
        return cls(authserver=auth, worldserver=world, database=db, logging=log)
