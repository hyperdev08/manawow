"""Death -> ghost -> corpse reclaim flow."""
from __future__ import annotations

import asyncio
import sqlite3

from src.common.config import AuthConfig, Config, DBConfig, LogConfig, WorldConfig
from src.world import character_db, graveyard
from src.world.realmsocket import WorldSocket


class FakeWriter:
    def __init__(self):
        self.writes = []

    def write(self, data):
        self.writes.append(bytes(data))

    async def drain(self):
        pass

    def get_extra_info(self, name):
        return ("127.0.0.1", 12345)

    def close(self):
        pass

    async def wait_closed(self):
        pass

    def is_closing(self):
        return False


def _make_world(tmp_path) -> tuple[Config, int]:
    db = str(tmp_path / "world.db")
    character_db.init_character_db(db)
    graveyard.init_graveyard_db(db)
    con = sqlite3.connect(db)
    try:
        con.execute(
            "INSERT INTO graveyard_locations(id, map, x, y, z, o, name)"
            " VALUES(1, 0, -8935.33, -188.646, 80.4165, 2.7, 'Northshire')")
        con.execute(
            "INSERT INTO graveyard_zones(id, ghost_loc, link_kind, faction)"
            " VALUES(1, 12, 0, 469)")
        con.commit()
    finally:
        con.close()
    cfg = Config(
        authserver=AuthConfig(), worldserver=WorldConfig(client_data=""),
        database=DBConfig(auth_path=str(tmp_path / "auth.db"), world_path=db),
        logging=LogConfig(level="WARNING", log_dir=str(tmp_path / "logs"),
                          console=False),
    )
    guid = character_db.create_character(
        db, 1, "Ghostguy", 1, 1, 0, 0, 0, 0, 0, 0)
    return cfg, guid


def test_death_release_and_reclaim(tmp_path):
    cfg, guid = _make_world(tmp_path)
    db = cfg.database.world_path

    async def flow():
        writer = FakeWriter()
        socket = WorldSocket(asyncio.StreamReader(), writer, cfg)
        socket.in_world = True
        socket.player_guid = guid
        socket.character = dict(character_db.get_character(db, 1, guid))
        socket._apply_combat_stats()
        socket.character["health"] = 0
        socket._get_combat_state()["dead"] = True

        await socket._record_death()
        assert socket.character["death_state"] == 1
        corpse = character_db.get_character_corpse(db, guid)
        assert corpse is not None and corpse["map"] == 0
        grave = socket._get_combat_state()["death_graveyard"]
        assert grave is not None and grave["id"] == 1

        await socket._release_spirit()
        assert socket.character["death_state"] == 2
        assert socket._get_combat_state()["ghost"] is True
        assert socket.character["map"] == 0
        assert abs(float(socket.character["x"]) + 8935.33) < 0.1
        assert character_db.get_character(db, 1, guid)["death_state"] == 2

        await socket._resurrect_player(at_corpse=True)
        assert socket.character["death_state"] == 0
        assert socket._get_combat_state()["dead"] is False
        assert socket._get_combat_state()["ghost"] is False
        assert int(socket.character["health"]) >= 1
        assert character_db.get_character_corpse(db, guid) is None
        assert writer.writes

    asyncio.run(flow())


def test_login_restores_a_persisted_ghost(tmp_path):
    cfg, guid = _make_world(tmp_path)
    db = cfg.database.world_path
    character_db.save_character_corpse(
        db, guid, 0, 12, -100.0, -200.0, 80.0, 1.0)
    character_db.set_character_death_state(db, guid, 2)

    async def flow():
        socket = WorldSocket(asyncio.StreamReader(), FakeWriter(), cfg)
        socket.in_world = True
        socket.player_guid = guid
        socket.character = dict(character_db.get_character(db, 1, guid))
        socket._apply_combat_stats()
        await socket._restore_death_state()
        combat = socket._get_combat_state()
        assert combat["dead"] is True
        assert combat["ghost"] is True
        assert int(socket.character["health"]) == 1
        assert combat["corpse"]["x"] == -100.0

    asyncio.run(flow())


def test_spirit_healer_resurrects_a_ghost(tmp_path):
    cfg, guid = _make_world(tmp_path)
    db = cfg.database.world_path

    async def flow():
        writer = FakeWriter()
        socket = WorldSocket(asyncio.StreamReader(), writer, cfg)
        socket.in_world = True
        socket.player_guid = guid
        socket.character = dict(character_db.get_character(db, 1, guid))
        socket._apply_combat_stats()
        socket.character.update({"death_state": 2, "map": 0,
                                 "x": -8935.33, "y": -188.646, "z": 80.4165})
        socket._get_combat_state()["dead"] = True
        socket._get_combat_state()["ghost"] = True
        socket._creature_low = lambda value: 77
        socket._get_creature_spawn = lambda low: {
            "guid": 77, "x": -8935.33, "y": -188.646, "z": 80.4165,
            "npc_flags": 16385,
        }
        await socket.handle_spirit_healer_activate(
            (0xF13000000000004D).to_bytes(8, "little"))
        assert socket.character["death_state"] == 0
        combat = socket._get_combat_state()
        assert combat["dead"] is False and combat["ghost"] is False
        assert int(socket.character["health"]) >= 1
        assert character_db.get_character(db, 1, guid)["death_state"] == 0
        assert writer.writes

    asyncio.run(flow())
