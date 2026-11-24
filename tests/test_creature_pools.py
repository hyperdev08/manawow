"""Creature pools: only max_limit members of a pool are spawned."""
from __future__ import annotations

import asyncio
import sqlite3
import struct

from src.auth.server import AuthServer
from src.common.config import AuthConfig, Config, DBConfig, LogConfig, WorldConfig
from src.crypto.srp6 import SRP6
from src.protocol.opcodes import Opcodes
from src.world import creature
from src.world import gameobject as go
from src.world.server import WorldServer
from tools import import_wotlk_db as importer
from tools import protocol_selftest as st
from tools.init_db import init_databases
from tests.test_gameobject import START


def test_build_pool_members():
    direct = [{"guid": "1", "pool_entry": "10", "chance": "0"},
              {"guid": "99", "pool_entry": "10", "chance": "0"}]      # not imported
    by_template = [{"id": "500", "pool_entry": "11", "chance": "10"}]
    spawns = [(1, 400), (2, 500), (3, 500), (4, 600)]
    assert importer.build_pool_members(direct, by_template, spawns) == [
        (1, 10, 0.0), (2, 11, 10.0), (3, 11, 10.0)]
    assert importer.build_pool_members(None, None, spawns) == []


def test_creature_pools_load_from_their_own_table(tmp_path):
    db = str(tmp_path / "world.db")
    go.init_gameobject_db(db)
    con = sqlite3.connect(db)
    con.executemany("INSERT INTO creature_pool_members(guid, pool, chance) VALUES(?,?,?)",
                    [(1, 10, 0), (2, 10, 0)])
    con.execute("INSERT INTO gameobject_pool_members(guid, pool, chance) VALUES(7, 20, 0)")
    con.execute("INSERT INTO pool_templates(entry, max_limit) VALUES(10, 1)")
    con.commit()
    con.close()
    pools = go.load_creature_pools(db)
    assert set(pools.member_pool) == {1, 2} and len(pools.active) == 1
    assert set(go.GameObjectPools.load(db).member_pool) == {7}


async def _flow(tmp_path):
    auth_db, world_db = str(tmp_path / "auth.db"), str(tmp_path / "world.db")
    cfg = Config(
        authserver=AuthConfig(host="127.0.0.1", port=0, realm_id=1, realm_name="T",
                              realm_address="127.0.0.1", realm_build_min=st.BUILD,
                              realm_build_max=st.BUILD),
        worldserver=WorldConfig(host="127.0.0.1", port=0, realm_id=1, motd="T"),
        database=DBConfig(auth_path=auth_db, world_path=world_db),
        logging=LogConfig(level="WARNING", log_dir=str(tmp_path / "logs"), console=False),
    )
    init_databases(cfg)
    near = creature.get_creature_spawns_near(world_db, 0, *START, 150.0)
    first, second = int(near[0]["guid"]), int(near[1]["guid"])
    con = sqlite3.connect(world_db)
    con.executemany("INSERT INTO creature_pool_members(guid, pool, chance) VALUES(?,?,?)",
                    [(first, 10, 0), (second, 10, 0)])
    con.execute("INSERT INTO pool_templates(entry, max_limit) VALUES(10, 1)")
    con.commit()
    con.close()
    salt, verifier = SRP6.make_registration_data(st.USERNAME, st.PASSWORD)
    con = sqlite3.connect(auth_db)
    con.execute("INSERT INTO account(username, v, s, expansion) VALUES(?,?,?,2)",
                (st.USERNAME, verifier, salt))
    con.commit()
    con.close()
    server = WorldServer(cfg)
    world = await asyncio.start_server(server.handle_client, "127.0.0.1", 0)
    cfg.worldserver.port = world.sockets[0].getsockname()[1]
    auth = await asyncio.start_server(AuthServer(cfg).handle_client, "127.0.0.1", 0)
    cfg.authserver.port = auth.sockets[0].getsockname()[1]
    writer = None
    try:
        session_key, _addr, realm_id = await st.authserver_flow("127.0.0.1", cfg.authserver.port)
        reader, writer, crypt = await st.world_handshake(
            "127.0.0.1", cfg.worldserver.port, st.USERNAME, session_key, realm_id)
        create = b"Pooler\x00" + bytes([1, 1, 0, 0, 0, 0, 0, 0, 0])
        await st.send_client_packet(writer, int(Opcodes.CMSG_CHAR_CREATE), create, crypt)
        await st.read_server_packet(reader, crypt)
        await st.send_client_packet(writer, int(Opcodes.CMSG_CHAR_ENUM), crypt=crypt)
        _op, enum_payload = await st.read_server_packet(reader, crypt)
        player = struct.unpack_from("<Q", enum_payload, 1)[0]
        await st.send_client_packet(writer, int(Opcodes.CMSG_PLAYER_LOGIN),
                                    struct.pack("<Q", player), crypt)
        burst = await st.read_login_burst(reader, crypt)
        shown = {creature.creature_low_guid(b["guid"]) for b in burst["creatures"]}
        assert len(shown & {first, second}) == 1          # one member of the pool
        assert len(shown) == len(near) - 1
        assert server.creature_pools is not None
    finally:
        if writer is not None:
            writer.close()
        world.close()
        auth.close()
        await world.wait_closed()
        await auth.wait_closed()


def test_only_one_pool_member_is_spawned(tmp_path):
    asyncio.run(_flow(tmp_path))


def test_spawn_groups_become_pools():
    tables = {
        "spawn_group": [
            {"Id": "19984", "Name": "Gruff Swiftbite", "Type": "0", "MaxCount": "1",
             "WorldState": "0"},
            {"Id": "19989", "Name": "Goldshire children - Day", "Type": "0",
             "MaxCount": "1", "WorldState": "9110"},          # needs world states
            {"Id": "19946", "Name": "Patrol", "Type": "0", "MaxCount": "0",
             "WorldState": "0"},                              # no limit: not a pool
            {"Id": "500", "Name": "Herbs", "Type": "1", "MaxCount": "2",
             "WorldState": "0"},
        ],
        "spawn_group_spawn": [
            {"Id": "19984", "Guid": "1", "Chance": "0"},
            {"Id": "19984", "Guid": "2", "Chance": "0"},
            {"Id": "19984", "Guid": "99", "Chance": "0"},      # not imported
            {"Id": "19989", "Guid": "3", "Chance": "0"},
            {"Id": "19946", "Guid": "4", "Chance": "0"},
            {"Id": "500", "Guid": "7", "Chance": "0"},
        ],
        "pool_creature": [{"guid": "2", "pool_entry": "42", "chance": "0"}],
    }
    creatures = [(1, 10), (2, 10), (3, 11), (4, 12)]
    members, templates = importer.build_creature_pool_rows(tables, creatures)
    base = importer.SPAWN_GROUP_POOL_BASE
    # guid 2 stays in its pool_creature pool; the group keeps guid 1.
    assert members == [(1, base + 19984, 0.0), (2, 42, 0.0)]
    assert templates == [(base + 19984, 1, "Gruff Swiftbite")]
    go_members, go_templates = importer.build_spawn_group_pools(
        tables, importer.SPAWN_GROUP_GAMEOBJECT, [(7, 1)])
    assert go_members == [(7, base + 500, 0.0)] and go_templates == [(base + 500, 2, "Herbs")]
