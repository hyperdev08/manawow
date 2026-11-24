"""Game object use: doors, chests, opening spells, loot, respawn and quest sparkle."""
from __future__ import annotations

import asyncio
import sqlite3
import struct

import pytest

from src.auth.server import AuthServer
from src.common.config import AuthConfig, Config, DBConfig, LogConfig, WorldConfig
from src.crypto.srp6 import SRP6
from src.protocol.opcodes import Opcodes
from src.world import gameobject as go
from src.world import object_update
from src.world import spells
from src.world import update_fields as F
from src.world.server import WorldServer
from tools import import_wotlk_db as importer
from tools import protocol_selftest as st
from tools.init_db import init_databases
from tests.test_gameobject import START, _insert, _spawn, _template

DOOR, CHEST, LOCKED, QUEST_CHEST = 501, 502, 503, 504
LOOT_ITEM = 159                  # Refreshing Spring Water
QUEST_ITEM = 11119               # Milly's Harvest
OPENING = 3365


# ----------------------------------------------------------------------
# Unit tests
# ----------------------------------------------------------------------
def test_type_specific_fields():
    door = _template(type=go.GAMEOBJECT_TYPE_DOOR, data1=12, data2=3000)
    assert go.lock_id(door) == 12 and go.auto_close_seconds(door) == 3.0
    assert go.loot_id(door) == 0 and not go.is_consumable(door)
    chest = _template(type=go.GAMEOBJECT_TYPE_CHEST, data0=43, data1=10119, data2=30,
                      data3=1, data8=3904)
    assert go.lock_id(chest) == 43 and go.loot_id(chest) == 10119
    assert go.is_consumable(chest) and go.restock_seconds(chest) == 30
    assert go.chest_quest_id(chest) == 3904
    assert go.lock_id(_template(type=go.GAMEOBJECT_TYPE_GENERIC, data0=9)) == 0


@pytest.mark.parametrize("low, high, expected", [(120, 120, {120}), (0, 0, {5}),
                                                 (10, 12, {10, 11, 12})])
def test_respawn_delay(low, high, expected):
    assert {go.respawn_delay(_spawn(spawn_time_min=low, spawn_time_max=high))
            for _ in range(40)} <= expected


def test_runtime_despawn_state_and_restock():
    rt = go.GameObjectRuntime()
    rt.loot[7] = {"items": [], "money": 0}
    rt.despawn(7, 10, now=100.0)
    assert not rt.is_spawned(7, now=105.0) and 7 not in rt.loot
    assert rt.is_spawned(7, now=110.0) and 7 not in rt.despawned

    rt.set_state(8, go.GO_STATE_ACTIVE, reset_after=3.0, now=0.0)
    rt.set_state(9, go.GO_STATE_ACTIVE, now=0.0)          # never resets
    assert rt.state(8) == go.GO_STATE_ACTIVE
    assert rt.expired_states(now=2.0) == []
    assert rt.expired_states(now=3.5) == [8]
    assert rt.state(8) is None and rt.state(9) == go.GO_STATE_ACTIVE

    rt.loot[10] = {"items": [(1, 1)], "money": 0}
    rt.restock[10] = 50.0
    assert rt.stored_loot(10, now=49.0) is not None
    assert rt.stored_loot(10, now=50.0) is None


def test_quest_loot_items_follow_references(tmp_path):
    db = str(tmp_path / "world.db")
    from src.world import creature
    creature.init_creature_db(db)
    go.init_gameobject_db(db)
    con = sqlite3.connect(db)
    con.executemany(
        "INSERT INTO gameobject_loot_template(entry, item, chance_or_quest_chance, "
        "group_id, mincount_or_ref, maxcount) VALUES(?,?,?,?,?,?)",
        [(1, 11119, -100.0, 0, 1, 1), (1, 159, 50.0, 0, 1, 1), (1, 0, 100.0, 0, -77, 1)])
    con.execute("INSERT INTO reference_loot_template(entry, item, chance_or_quest_chance, "
                "group_id, mincount_or_ref, maxcount) VALUES(77, 2589, -35.5, 0, 1, 1)")
    con.commit()
    con.close()
    assert go.quest_loot_items(db, 1) == {11119, 2589}
    assert go.quest_loot_items(db, 2) == frozenset()
    go.clear_cache()


def test_spell_target_data_marks_game_objects():
    cast = spells.SpellCast(1, OPENING, 0)
    object_guid = go.gameobject_guid(26743, 161557)
    data = spells._target_data(object_guid, cast)
    assert struct.unpack_from("<I", data)[0] == 0x800
    assert struct.unpack_from("<I", spells._target_data(0xF130000000000001, cast))[0] == 0x02


def test_loot_chances_keep_their_fractions(tmp_path):
    db = str(tmp_path / "world.db")
    from src.world import creature
    creature.init_creature_db(db)
    go.init_gameobject_db(db)
    con = sqlite3.connect(db)
    rows = [{"entry": "38", "item": "159", "ChanceOrQuestChance": "6.4011", "groupid": "0",
             "mincountOrRef": "1", "maxcount": "3", "condition_id": "0", "comments": "Water"},
            {"entry": "30", "item": "44007", "ChanceOrQuestChance": "0.5", "groupid": "0",
             "mincountOrRef": "-44007", "maxcount": "1", "condition_id": "0", "comments": ""}]
    for table in ("creature_loot_template", "gameobject_loot_template"):
        assert importer.insert_loot_table(con, table, rows) == 2
        chances = sorted(r[0] for r in con.execute(
            f"SELECT chance_or_quest_chance FROM {table}"))
        assert chances == [0.5, pytest.approx(6.4011)]
    con.close()


# ----------------------------------------------------------------------
# End to end
# ----------------------------------------------------------------------
def _world_rows():
    x, y, z = START
    templates = [
        _template(entry=DOOR, type=go.GAMEOBJECT_TYPE_DOOR, name="Door", data2=2000),
        _template(entry=CHEST, type=go.GAMEOBJECT_TYPE_CHEST, name="Crate",
                  data1=CHEST, data3=1, min_gold=7, max_gold=7),
        _template(entry=LOCKED, type=go.GAMEOBJECT_TYPE_CHEST, name="Locked Box",
                  data0=57, data1=CHEST, data3=1),
        _template(entry=QUEST_CHEST, type=go.GAMEOBJECT_TYPE_CHEST, name="Milly's Harvest",
                  data1=QUEST_CHEST, data3=1),
    ]
    spawns = [
        _spawn(guid=1, entry=DOOR, x=x + 2, y=y, z=z, type=go.GAMEOBJECT_TYPE_DOOR),
        _spawn(guid=2, entry=CHEST, x=x + 2, y=y + 1, z=z, spawn_time_min=300,
               spawn_time_max=300),
        _spawn(guid=3, entry=LOCKED, x=x + 2, y=y - 1, z=z),
        _spawn(guid=4, entry=QUEST_CHEST, x=x - 2, y=y, z=z),
        _spawn(guid=5, entry=CHEST, x=x + 60, y=y, z=z),          # visible, too far to use
    ]
    return templates, spawns


def _prepare(world_db: str) -> None:
    con = sqlite3.connect(world_db)
    _insert(con, *_world_rows())
    con.executemany(
        "INSERT INTO gameobject_loot_template(entry, item, chance_or_quest_chance, "
        "group_id, mincount_or_ref, maxcount) VALUES(?,?,?,?,?,?)",
        [(CHEST, LOOT_ITEM, 100.0, 0, 2, 2), (QUEST_CHEST, QUEST_ITEM, -100.0, 0, 1, 1)])
    con.executemany("INSERT OR REPLACE INTO item_templates(entry, name1, display_id, stackable) "
                    "VALUES(?, ?, ?, 20)",
                    [(QUEST_ITEM, "Milly's Harvest", 7395),
                     (LOOT_ITEM, "Refreshing Spring Water", 18084)])
    con.execute("INSERT OR REPLACE INTO spell_templates(id, name, effect1, "
                "effect_misc_value1, casting_time_index, range_index) "
                "VALUES(?, 'Opening', 33, 5, 1, 12)", (OPENING,))
    # A quest that needs the harvest, so the quest chest lights up.
    con.execute("INSERT OR REPLACE INTO quest_templates(entry, title, min_level, quest_level, "
                "req_item_id1, req_item_count1) VALUES(3904, 'Milly''s Harvest', 1, 3, ?, 8)",
                (QUEST_ITEM,))
    con.commit()
    con.close()


def _guid(low, entry):
    return go.gameobject_guid(low, entry)


async def _read_until(reader, crypt, wanted, timeout=5):
    seen = []
    while True:
        opcode, payload = await asyncio.wait_for(st.read_server_packet(reader, crypt), timeout)
        seen.append((opcode, payload))
        if opcode in wanted:
            return opcode, payload, seen


def _values_update(payload: bytes) -> tuple[int, dict]:
    """Decode a single uncompressed UPDATETYPE_VALUES block."""
    assert struct.unpack_from("<I", payload)[0] == 1 and payload[4] == 0
    pos = 5
    mask_byte = payload[pos]
    pos += 1
    guid = 0
    for i in range(8):
        if mask_byte & (1 << i):
            guid |= payload[pos] << (i * 8)
            pos += 1
    blocks = payload[pos]
    pos += 1
    mask = payload[pos:pos + blocks * 4]
    pos += blocks * 4
    values = {}
    for field in range(blocks * 32):
        if mask[field // 8] & (1 << (field % 8)):
            values[field] = struct.unpack_from("<I", payload, pos)[0]
            pos += 4
    return guid, values


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
    _prepare(world_db)
    go.clear_cache()
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
        create = b"Opener\x00" + bytes([1, 1, 0, 0, 0, 0, 0, 0, 0])
        await st.send_client_packet(writer, int(Opcodes.CMSG_CHAR_CREATE), create, crypt)
        await st.read_server_packet(reader, crypt)
        await st.send_client_packet(writer, int(Opcodes.CMSG_CHAR_ENUM), crypt=crypt)
        _op, enum_payload = await st.read_server_packet(reader, crypt)
        player = struct.unpack_from("<Q", enum_payload, 1)[0]
        await st.send_client_packet(writer, int(Opcodes.CMSG_PLAYER_LOGIN),
                                    struct.pack("<Q", player), crypt)
        burst = await st.read_login_burst(reader, crypt)
        objects = {b["guid"]: b for b in burst["gameobjects"]}
        assert set(objects) == {_guid(1, DOOR), _guid(2, CHEST), _guid(3, LOCKED),
                                _guid(4, QUEST_CHEST), _guid(5, CHEST)}
        # No quest yet: the quest chest is not activated.
        assert objects[_guid(4, QUEST_CHEST)]["values"].get(F.GAMEOBJECT_DYNAMIC, 0) == 0

        async def send(opcode, payload=b""):
            await st.send_client_packet(writer, int(opcode), payload, crypt)

        # -- door: opens for everyone, closes by itself ----------------
        await send(Opcodes.CMSG_GAMEOBJ_USE, struct.pack("<Q", _guid(1, DOOR)))
        _op, payload, _ = await _read_until(
            reader, crypt, {Opcodes.SMSG_UPDATE_OBJECT, Opcodes.SMSG_COMPRESSED_UPDATE_OBJECT})
        guid, values = _values_update(payload)
        assert guid == _guid(1, DOOR)
        assert values[F.GAMEOBJECT_BYTES_1] & 0xFF == go.GO_STATE_ACTIVE
        assert server.gameobjects.state(1) == go.GO_STATE_ACTIVE

        # -- unlocked chest: loot, take, despawn -----------------------
        await send(Opcodes.CMSG_GAMEOBJ_USE, struct.pack("<Q", _guid(2, CHEST)))
        _op, payload, _ = await _read_until(reader, crypt, {Opcodes.SMSG_LOOT_RESPONSE})
        loot_guid, _method, money, count = struct.unpack_from("<QBIB", payload)
        assert (loot_guid, money, count) == (_guid(2, CHEST), 7, 1)
        assert struct.unpack_from("<BII", payload, 14) == (0, LOOT_ITEM, 2)
        await send(Opcodes.CMSG_LOOT_MONEY)
        await _read_until(reader, crypt, {Opcodes.SMSG_LOOT_MONEY_NOTIFY})
        await send(Opcodes.CMSG_AUTOSTORE_LOOT_ITEM, bytes([0]))
        _op, payload, _ = await _read_until(reader, crypt, {Opcodes.SMSG_DESTROY_OBJECT})
        assert struct.unpack_from("<Q", payload)[0] == _guid(2, CHEST)
        assert not server.gameobjects.is_spawned(2)
        con = sqlite3.connect(world_db)
        stacks = con.execute("SELECT SUM(stack_count) FROM character_items WHERE "
                             "character_guid = ? AND item_entry = ?",
                             (player, LOOT_ITEM)).fetchone()[0]
        money_now = con.execute("SELECT money FROM characters WHERE guid = ?",
                                (player,)).fetchone()[0]
        con.close()
        assert stacks >= 2 and money_now == 7

        # -- a despawned or distant object cannot be used ------------
        await send(Opcodes.CMSG_GAMEOBJ_USE, struct.pack("<Q", _guid(2, CHEST)))
        await send(Opcodes.CMSG_GAMEOBJ_USE, struct.pack("<Q", _guid(5, CHEST)))
        # -- locked box: a click is ignored, the Opening spell opens it
        await send(Opcodes.CMSG_GAMEOBJ_USE, struct.pack("<Q", _guid(3, LOCKED)))
        target = struct.pack("<I", 0x800) + object_update.pack_guid(_guid(3, LOCKED))
        await send(Opcodes.CMSG_CAST_SPELL,
                   struct.pack("<BIB", 1, OPENING, 0) + target)
        _op, payload, seen = await _read_until(reader, crypt, {Opcodes.SMSG_LOOT_RESPONSE})
        assert struct.unpack_from("<Q", payload)[0] == _guid(3, LOCKED)
        assert Opcodes.SMSG_SPELL_GO in [op for op, _ in seen]
        # Nothing else answered with loot before the spell went off.
        assert [op for op, _ in seen].count(Opcodes.SMSG_LOOT_RESPONSE) == 1
        # Closing the window with loot left keeps the box for later.
        await send(Opcodes.CMSG_LOOT_RELEASE, struct.pack("<Q", _guid(3, LOCKED)))
        await _read_until(reader, crypt, {Opcodes.SMSG_LOOT_RELEASE_RESPONSE})
        assert server.gameobjects.is_spawned(3)
        assert server.gameobjects.loot[3]["items"] == [(LOOT_ITEM, 2)]

        # -- quest chest: closed without the quest, sparkles with it ---
        await send(Opcodes.CMSG_GAMEOBJ_USE, struct.pack("<Q", _guid(4, QUEST_CHEST)))
        _op, payload, _ = await _read_until(reader, crypt, {Opcodes.SMSG_LOOT_RESPONSE})
        assert struct.unpack_from("<QBIB", payload)[3] == 0          # no quest, no loot
        await send(Opcodes.CMSG_LOOT_RELEASE, struct.pack("<Q", _guid(4, QUEST_CHEST)))
        await _read_until(reader, crypt, {Opcodes.SMSG_LOOT_RELEASE_RESPONSE})
        session = next(s for s in server.sessions if s.player_guid == player)
        from src.world import quests as Q
        quest = Q.get_quest_template(world_db, 3904)
        assert await session._accept_quest(quest)
        _op, payload, _ = await _read_until(
            reader, crypt, {Opcodes.SMSG_UPDATE_OBJECT, Opcodes.SMSG_COMPRESSED_UPDATE_OBJECT})
        while _values_update(payload)[0] != _guid(4, QUEST_CHEST):
            _op, payload, _ = await _read_until(
                reader, crypt, {Opcodes.SMSG_UPDATE_OBJECT, Opcodes.SMSG_COMPRESSED_UPDATE_OBJECT})
        assert _values_update(payload)[1][F.GAMEOBJECT_DYNAMIC] == (
            go.GO_DYNFLAG_LO_ACTIVATE | go.GO_DYNFLAG_LO_SPARKLE)
        await send(Opcodes.CMSG_GAMEOBJ_USE, struct.pack("<Q", _guid(4, QUEST_CHEST)))
        _op, payload, _ = await _read_until(reader, crypt, {Opcodes.SMSG_LOOT_RESPONSE})
        assert struct.unpack_from("<QBIB", payload)[3] == 1
        assert struct.unpack_from("<BII", payload, 14) == (0, QUEST_ITEM, 1)
    finally:
        if writer is not None:
            writer.close()
        world.close()
        auth.close()
        await world.wait_closed()
        await auth.wait_closed()
        go.clear_cache()


def test_use_doors_chests_and_quest_chests(tmp_path):
    asyncio.run(_flow(tmp_path))


# ----------------------------------------------------------------------
# Pools
# ----------------------------------------------------------------------
def test_pool_keeps_max_limit_members_active():
    pools = go.GameObjectPools(
        members=[(1, 10, 0), (2, 10, 0), (3, 10, 0), (4, 11, 0), (5, 11, 0)],
        limits={10: 1, 11: 0})                    # 0 = no limit
    assert len({1, 2, 3} & pools.active) == 1
    assert {4, 5} <= pools.active
    assert not pools.is_hidden(99)                # not pooled
    hidden = {1, 2, 3} - pools.active
    assert all(pools.is_hidden(g) for g in hidden)


def test_pool_rerolls_after_despawn():
    pools = go.GameObjectPools(members=[(1, 10, 0), (2, 10, 0)], limits={10: 1})
    first = next(iter(pools.active))
    pools.on_despawn(first, respawn_at=100.0)
    assert pools.active == set()
    assert pools.due(99.0) == []
    spawned = pools.due(100.0)
    assert len(spawned) == 1 and pools.active == set(spawned)
    assert pools.pending == []


def test_nested_pools_spawn_through_the_mother():
    # Mother pool 1 picks one of the child pools 2/3; each child shows one chest.
    pools = go.GameObjectPools(
        members=[(10, 2, 0), (11, 2, 0), (20, 3, 0), (21, 3, 0)],
        limits={1: 1, 2: 1, 3: 1},
        children=[(2, 1, 0), (3, 1, 0)])
    assert len(pools.active) == 1
    assert pools.active <= {10, 11, 20, 21}


def test_explicit_pool_chances():
    picks = {go._roll_member([(1, 100.0), (2, 0.0)]) for _ in range(50)}
    assert picks == {1}


def test_runtime_hides_inactive_pool_members():
    rt = go.GameObjectRuntime()
    rt.pools = go.GameObjectPools(members=[(1, 10, 0), (2, 10, 0)], limits={10: 1})
    active = next(iter(rt.pools.active))
    other = ({1, 2} - {active}).pop()
    assert rt.is_spawned(active) and not rt.is_spawned(other)
    rt.despawn(active, 5, now=0.0)
    assert not rt.is_spawned(active, now=1.0)
    assert rt.respawn_pools(now=5.0)
    assert sum(rt.is_spawned(g, now=5.0) for g in (1, 2)) == 1


def test_build_pool_rows():
    tables = {
        "pool_gameobject": [{"guid": "1", "pool_entry": "10", "chance": "0"},
                            {"guid": "99", "pool_entry": "10", "chance": "0"}],
        "pool_gameobject_template": [{"id": "500", "pool_entry": "11", "chance": "25.5"}],
        "pool_template": [{"entry": "10", "max_limit": "1", "description": "Chests"}],
        "pool_pool": [{"pool_id": "11", "mother_pool": "12", "chance": "0"}],
    }
    spawn_rows = [(1, 400), (2, 500), (3, 500)]
    rows = importer.build_gameobject_pool_rows(tables, spawn_rows)
    assert rows["gameobject_pool_members"] == [(1, 10, 0.0), (2, 11, 25.5), (3, 11, 25.5)]
    assert rows["pool_templates"] == [(10, 1, "Chests")]
    assert rows["pool_pools"] == [(11, 12, 0.0)]
