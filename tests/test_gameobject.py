"""Game objects: guids, rotation, packets, visibility grid, import and login."""
from __future__ import annotations

import asyncio
import math
import sqlite3
import struct

import pytest

from src.auth.server import AuthServer
from src.common.config import AuthConfig, Config, DBConfig, LogConfig, WorldConfig
from src.crypto.srp6 import SRP6
from src.protocol.opcodes import Opcodes
from src.world import gameobject as go
from src.world import update_fields as F
from src.world.server import WorldServer
from tools import import_wotlk_db as importer
from tools import protocol_selftest as st
from tools.init_db import init_databases

START = (-8949.95, -132.49, 83.53)                  # human warrior start


def _template(**kw):
    base = {"entry": 1, "type": go.GAMEOBJECT_TYPE_GENERIC, "display_id": 100,
            "name": "Thing", "icon_name": "", "cast_bar_caption": "", "unk1": "",
            "faction": 0, "flags": 0, "extra_flags": 0, "size": 1.0,
            "min_gold": 0, "max_gold": 0, "script_name": ""}
    base.update({f"data{i}": 0 for i in range(24)})
    base.update({f"quest_item{i}": 0 for i in range(1, 7)})
    base.update(kw)
    return base


def _spawn(**kw):
    base = {"guid": 7, "entry": 1, "map": 0, "phase_mask": 1,
            "x": 1.0, "y": 2.0, "z": 3.0, "o": 0.5,
            "rotation0": 0.0, "rotation1": 0.0, "rotation2": 0.0, "rotation3": 0.0,
            "spawn_time_min": 180, "spawn_time_max": 180,
            "anim_progress": 255, "state": -1,
            "type": go.GAMEOBJECT_TYPE_GENERIC, "display_id": 100, "flags": 0,
            "faction": 0, "size": 1.0, "data0": 0}
    base.update(kw)
    return base


def _signed(value: int, bits: int) -> int:
    return value - (1 << bits) if value >= (1 << (bits - 1)) else value


def _unpack_rotation(packed: int):
    """Inverse of TrinityCore ``UpdatePackedRotation``: 22/21/21-bit x/y/z."""
    packed &= (1 << 64) - 1
    x = _signed((packed >> 42) & ((1 << 22) - 1), 22) / (1 << 21)
    y = _signed((packed >> 21) & ((1 << 21) - 1), 21) / (1 << 20)
    z = _signed(packed & ((1 << 21) - 1), 21) / (1 << 20)
    return x, y, z


# ----------------------------------------------------------------------
# Unit tests
# ----------------------------------------------------------------------
def test_guid_packs_entry_and_counter():
    guid = go.gameobject_guid(26743, 176573)
    assert guid >> 48 == 0xF110
    assert go.gameobject_low_guid(guid) == 26743
    assert go.gameobject_entry_from_guid(guid) == 176573
    assert go.is_gameobject_guid(guid)
    assert not go.is_gameobject_guid(0xF130000000000001)


def test_rotation_from_orientation_when_quaternion_missing():
    qx, qy, qz, qw = go.spawn_rotation(_spawn(o=math.pi / 2))
    assert (qx, qy) == (0.0, 0.0)
    assert qz == pytest.approx(math.sin(math.pi / 4))
    assert qw == pytest.approx(math.cos(math.pi / 4))


def test_rotation_quaternion_is_normalised():
    quat = go.spawn_rotation(_spawn(rotation2=0.0, rotation3=2.0))
    assert quat == pytest.approx((0.0, 0.0, 0.0, 1.0))


@pytest.mark.parametrize("quat", [
    (0.0, 0.0, 0.0, 1.0),
    (0.0, 0.0, -0.788, 0.616),
    (0.0, 0.0, 0.984, 0.178),
    (0.0, 0.0, 0.5, -0.866),       # negative w flips the stored sign
    (0.1, -0.2, 0.3, 0.927),
])
def test_packed_rotation_round_trips(quat):
    packed = go.packed_rotation(quat)
    assert -(1 << 63) <= packed < (1 << 63)
    sign = 1 if quat[3] >= 0 else -1
    x, y, z = _unpack_rotation(packed)
    assert x == pytest.approx(quat[0] * sign, abs=1e-5)
    assert y == pytest.approx(quat[1] * sign, abs=1e-5)
    assert z == pytest.approx(quat[2] * sign, abs=1e-5)


@pytest.mark.parametrize("kw, state", [
    ({}, go.GO_STATE_READY),
    ({"state": 0}, go.GO_STATE_ACTIVE),
    ({"state": 2}, go.GO_STATE_ACTIVE_ALTERNATIVE),
    ({"type": go.GAMEOBJECT_TYPE_DOOR, "data0": 1}, go.GO_STATE_ACTIVE),   # startOpen
    ({"type": go.GAMEOBJECT_TYPE_DOOR, "data0": 0}, go.GO_STATE_READY),
    ({"type": go.GAMEOBJECT_TYPE_CHEST, "data0": 57}, go.GO_STATE_READY),  # data0 = lock
])
def test_initial_state(kw, state):
    assert go.initial_state(_spawn(**kw)) == state


def test_create_block_layout():
    spawn = _spawn(guid=26743, entry=176573, display_id=3972, faction=35,
                   flags=0x20, size=1.5, rotation2=0.91, rotation3=0.415,
                   x=-8901.5, y=-120.25, z=81.5, o=2.29)
    block = go.build_gameobject_create_block(spawn)
    obj = _parse_block(block)
    assert obj["update_type"] == go.UPDATETYPE_CREATE_OBJECT
    assert obj["guid"] == go.gameobject_guid(26743, 176573)
    assert obj["type_id"] == go.TYPEID_GAMEOBJECT
    assert obj["flags"] == 0x0250
    assert obj["position"] == pytest.approx((-8901.5, -120.25, 81.5, 2.29), abs=1e-4)
    assert obj["high_guid"] == 26743          # the LOWGUID word is the counter
    assert obj["rotation"] == go.packed_rotation(go.spawn_rotation(spawn))
    assert obj["mask_blocks"] == 1
    values = obj["values"]
    assert values[F.OBJECT_FIELD_TYPE] == F.TYPEMASK_OBJECT | F.TYPEMASK_GAMEOBJECT
    assert values[F.OBJECT_FIELD_ENTRY] == 176573
    assert struct.unpack("<f", struct.pack("<I", values[F.OBJECT_FIELD_SCALE_X]))[0] == 1.5
    assert values[F.GAMEOBJECT_DISPLAYID] == 3972
    assert values[F.GAMEOBJECT_FLAGS] == 0x20
    assert values[F.GAMEOBJECT_FACTION] == 35
    assert struct.unpack("<f", struct.pack("<I", values[F.GAMEOBJECT_PARENTROTATION + 3]))[0] == 1.0
    state, go_type, _art, anim = values[F.GAMEOBJECT_BYTES_1].to_bytes(4, "little")
    assert (state, go_type, anim) == (go.GO_STATE_READY, go.GAMEOBJECT_TYPE_GENERIC, 255)


def test_traps_use_create_object2():
    block = go.build_gameobject_create_block(_spawn(type=go.GAMEOBJECT_TYPE_TRAP))
    assert block[0] == go.UPDATETYPE_CREATE_OBJECT2


def _parse_block(block: bytes) -> dict:
    """Decode one game object create block the way the 3.3.5 client does."""
    pos = 0
    obj = {"update_type": block[pos]}
    pos += 1
    guid_mask = block[pos]
    pos += 1
    guid = 0
    for i in range(8):
        if guid_mask & (1 << i):
            guid |= block[pos] << (i * 8)
            pos += 1
    obj["guid"] = guid
    obj["type_id"] = block[pos]
    pos += 1
    obj["flags"] = struct.unpack_from("<H", block, pos)[0]
    pos += 2
    obj["position"] = struct.unpack_from("<ffff", block, pos)
    pos += 16
    obj["high_guid"] = struct.unpack_from("<I", block, pos)[0]
    pos += 4
    obj["rotation"] = struct.unpack_from("<q", block, pos)[0]
    pos += 8
    obj["mask_blocks"] = block[pos]
    pos += 1
    mask = block[pos:pos + obj["mask_blocks"] * 4]
    pos += obj["mask_blocks"] * 4
    values = {}
    for field in range(obj["mask_blocks"] * 32):
        if mask[field // 8] & (1 << (field % 8)):
            values[field] = struct.unpack_from("<I", block, pos)[0]
            pos += 4
    assert pos == len(block)
    obj["values"] = values
    return obj


def test_update_packet_compresses_large_bodies():
    assert go.build_gameobject_update([]) is None
    opcode, payload = go.build_gameobject_update([_spawn(guid=g) for g in range(1, 6)])
    assert opcode == Opcodes.SMSG_COMPRESSED_UPDATE_OBJECT
    import zlib
    body = zlib.decompress(payload[4:])
    assert struct.unpack_from("<I", body)[0] == 5


def test_query_response_layout():
    template = _template(entry=176573, type=5, display_id=3972, name="Alliance Bell",
                         icon_name="Point", cast_bar_caption="Ringing", size=1.25,
                         data0=4294967295, data1=-1, quest_item1=745)
    payload = go.build_gameobject_query_response(176573, template)
    entry, go_type, display = struct.unpack_from("<III", payload, 0)
    assert (entry, go_type, display) == (176573, 5, 3972)
    pos = 12
    strings = []
    for _ in range(7):
        end = payload.index(b"\x00", pos)
        strings.append(payload[pos:end].decode())
        pos = end + 1
    assert strings == ["Alliance Bell", "", "", "", "Point", "Ringing", ""]
    data = struct.unpack_from("<24I", payload, pos)
    assert data[0] == 0xFFFFFFFF and data[1] == 0xFFFFFFFF
    pos += 96
    assert struct.unpack_from("<f", payload, pos)[0] == 1.25
    assert struct.unpack_from("<6I", payload, pos + 4) == (745, 0, 0, 0, 0, 0)
    assert len(payload) == pos + 4 + 24


def test_query_response_not_found():
    assert go.build_gameobject_query_response(42, None) == struct.pack("<I", 42 | 0x80000000)


# ----------------------------------------------------------------------
# Database: grid and import
# ----------------------------------------------------------------------
def _insert(con, template_rows=(), spawn_rows=()):
    for table, columns, rows in (
        ("gameobject_templates", go.TEMPLATE_COLUMNS, template_rows),
        ("gameobject_spawns", go.SPAWN_COLUMNS, spawn_rows),
    ):
        for row in rows:
            con.execute(
                f"INSERT INTO {table}({', '.join(columns)}) "
                f"VALUES({', '.join('?' for _ in columns)})",
                tuple(row[c] for c in columns))
    con.commit()


def test_grid_filters_and_radius(tmp_path):
    db = str(tmp_path / "world.db")
    go.init_gameobject_db(db)
    con = sqlite3.connect(db)
    _insert(
        con,
        [_template(entry=1), _template(entry=2, display_id=0),
         _template(entry=3, type=go.GAMEOBJECT_TYPE_TRANSPORT)],
        [_spawn(guid=1, entry=1, x=0, y=0, z=0),
         _spawn(guid=2, entry=1, x=100, y=0, z=0),
         _spawn(guid=3, entry=1, x=200, y=0, z=0),        # out of range
         _spawn(guid=4, entry=1, x=-30, y=-30, z=0),      # other cell, negative
         _spawn(guid=5, entry=1, x=5, y=0, z=0, phase_mask=2),
         _spawn(guid=6, entry=1, x=5, y=0, z=0, phase_mask=3),
         _spawn(guid=7, entry=2, x=5, y=0, z=0),          # no model
         _spawn(guid=8, entry=3, x=5, y=0, z=0),          # elevator
         _spawn(guid=9, entry=1, x=5, y=0, z=0, map=1)],
    )
    con.close()
    assert not go.is_map_loaded(db, 0)
    near = go.gameobjects_near(db, 0, 0.0, 0.0, 0.0, 150.0)
    assert go.is_map_loaded(db, 0)
    assert [s["guid"] for s in near] == [1, 2, 4, 6]
    assert near[0]["display_id"] == 100 and near[0]["type"] == go.GAMEOBJECT_TYPE_GENERIC
    assert [s["guid"] for s in go.gameobjects_near(db, 1, 0, 0, 0, 150.0)] == [9]
    assert go.gameobjects_near(db, 0, 0, 0, 0, 0) == []
    assert go.get_gameobject_template(db, 1)["name"] == "Thing"
    assert go.get_gameobject_template(db, 99) is None
    go.clear_cache()
    assert not go.is_map_loaded(db, 0)


def test_world_event_spawns_are_hidden(tmp_path):
    db = str(tmp_path / "world.db")
    go.init_gameobject_db(db)
    con = sqlite3.connect(db)
    _insert(con, [_template(entry=1)],
            [_spawn(guid=1, entry=1), _spawn(guid=2, entry=1), _spawn(guid=3, entry=1)])
    # 2 = Winter Veil decoration (only during the event), 3 = removed during one.
    con.executemany("INSERT INTO gameobject_events(guid, event) VALUES(?, ?)",
                    [(2, 2), (3, -12)])
    con.commit()
    con.close()
    assert [s["guid"] for s in go.gameobjects_near(db, 0, 1.0, 2.0, 3.0)] == [1, 3]
    go.clear_cache()


def test_build_gameobject_event_rows():
    tables = {"game_event_gameobject": [
        {"guid": "1", "event": "2"}, {"guid": "1", "event": "2"},
        {"guid": "2", "event": "-12"}, {"guid": "99", "event": "7"},
        {"guid": "3", "event": "0"}]}
    spawn_rows = [(1,), (2,), (3,)]
    assert importer.build_gameobject_event_rows(tables, spawn_rows) == [(1, 2), (2, -12)]


def test_build_gameobject_rows():
    tables = {
        "gameobject_template": [
            {"entry": "10", "type": "3", "displayId": "259", "name": "Chest",
             "IconName": "", "castBarCaption": "Opening", "unk1": "",
             "faction": "0", "flags": "4", "ExtraFlags": "0", "size": "0.8",
             "mingold": "5", "maxgold": "10", "ScriptName": "",
             **{f"questItem{i}": "0" for i in range(1, 7)},
             **{f"data{i}": str(i) for i in range(24)}},
            {"entry": "11", "type": "5", "displayId": "1", "name": "Other",
             "size": "0"},
        ],
        "gameobject": [
            {"guid": "1", "id": "10", "map": "0", "spawnMask": "1", "phaseMask": "1",
             "position_x": "1.5", "position_y": "2.5", "position_z": "3.5",
             "orientation": "0.25", "rotation0": "0", "rotation1": "0",
             "rotation2": "0.1", "rotation3": "0.99",
             "spawntimesecsmin": "180", "spawntimesecsmax": "300"},
            {"guid": "2", "id": "0", "map": "0", "spawnMask": "1", "phaseMask": "1"},
            {"guid": "3", "id": "99", "map": "0", "spawnMask": "1", "phaseMask": "1"},
            {"guid": "4", "id": "10", "map": "0", "spawnMask": "2", "phaseMask": "1"},
            {"guid": "5", "id": "10", "map": "0", "spawnMask": "1", "phaseMask": "2"},
            {"guid": "6", "id": "0", "map": "0", "spawnMask": "1", "phaseMask": "1"},
        ],
        "gameobject_addon": [{"guid": "1", "animprogress": "100", "state": "0"}],
        "gameobject_spawn_entry": [{"guid": "2", "entry": "11"},
                                   {"guid": "2", "entry": "10"}],
    }
    templates, spawns, skipped = importer.build_gameobject_rows(tables)
    rows = [dict(zip(go.TEMPLATE_COLUMNS, row)) for row in templates]
    assert rows[0]["cast_bar_caption"] == "Opening" and rows[0]["size"] == pytest.approx(0.8)
    assert rows[0]["data23"] == 23 and rows[0]["min_gold"] == 5
    assert rows[1]["size"] == 1.0                    # zero size defaults to 1
    spawn_rows = {row[0]: dict(zip(go.SPAWN_COLUMNS, row)) for row in spawns}
    assert sorted(spawn_rows) == [1, 2]
    first = spawn_rows[1]
    assert (first["x"], first["rotation3"], first["spawn_time_max"]) == (1.5, 0.99, 300)
    assert (first["anim_progress"], first["state"]) == (100, 0)
    assert spawn_rows[2]["entry"] == 10              # lowest spawn_entry
    assert (spawn_rows[2]["anim_progress"], spawn_rows[2]["state"]) == (255, -1)
    assert skipped == {"missing_template": 2, "spawn_mask": 1, "phase": 1}


# ----------------------------------------------------------------------
# End to end: the login burst carries the objects and queries are answered
# ----------------------------------------------------------------------
async def _login_flow(tmp_path):
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
    con = sqlite3.connect(world_db)
    _insert(con,
            [_template(entry=176573, name="Alliance Bell", display_id=3972)],
            [_spawn(guid=26743, entry=176573, x=START[0] + 5, y=START[1], z=START[2]),
             _spawn(guid=26744, entry=176573, x=START[0] + 400, y=START[1], z=START[2])])
    con.close()
    go.clear_cache()
    salt, verifier = SRP6.make_registration_data(st.USERNAME, st.PASSWORD)
    con = sqlite3.connect(auth_db)
    con.execute("INSERT INTO account(username, v, s, expansion) VALUES(?,?,?,2)",
                (st.USERNAME, verifier, salt))
    con.commit()
    con.close()

    world = await asyncio.start_server(WorldServer(cfg).handle_client, "127.0.0.1", 0)
    cfg.worldserver.port = world.sockets[0].getsockname()[1]
    auth = await asyncio.start_server(AuthServer(cfg).handle_client, "127.0.0.1", 0)
    cfg.authserver.port = auth.sockets[0].getsockname()[1]
    writer = None
    try:
        session_key, _addr, realm_id = await st.authserver_flow("127.0.0.1", cfg.authserver.port)
        reader, writer, crypt = await st.world_handshake(
            "127.0.0.1", cfg.worldserver.port, st.USERNAME, session_key, realm_id)
        create = b"Looker\x00" + bytes([1, 1, 0, 0, 0, 0, 0, 0, 0])
        await st.send_client_packet(writer, int(Opcodes.CMSG_CHAR_CREATE), create, crypt)
        await st.read_server_packet(reader, crypt)
        await st.send_client_packet(writer, int(Opcodes.CMSG_CHAR_ENUM), crypt=crypt)
        _op, enum_payload = await st.read_server_packet(reader, crypt)
        player = struct.unpack_from("<Q", enum_payload, 1)[0]
        await st.send_client_packet(writer, int(Opcodes.CMSG_PLAYER_LOGIN),
                                    struct.pack("<Q", player), crypt)
        burst = await st.read_login_burst(reader, crypt)
        assert [b["guid"] for b in burst["gameobjects"]] == [go.gameobject_guid(26743, 176573)]

        await st.send_client_packet(
            writer, int(Opcodes.CMSG_GAMEOBJECT_QUERY),
            struct.pack("<IQ", 176573, go.gameobject_guid(26743, 176573)), crypt)
        while True:
            opcode, payload = await asyncio.wait_for(st.read_server_packet(reader, crypt), 5)
            if opcode == Opcodes.SMSG_GAMEOBJECT_QUERY_RESPONSE:
                break
        assert struct.unpack_from("<I", payload)[0] == 176573
        assert b"Alliance Bell\x00" in payload
    finally:
        if writer is not None:
            writer.close()
        world.close()
        auth.close()
        await world.wait_closed()
        await auth.wait_closed()
        go.clear_cache()


def test_login_shows_nearby_gameobjects(tmp_path):
    asyncio.run(_login_flow(tmp_path))


# ----------------------------------------------------------------------
# World events also hide creatures
# ----------------------------------------------------------------------
def test_world_event_creatures_are_hidden(tmp_path):
    from src.world import creature
    db = str(tmp_path / "world.db")
    creature.init_creature_db(db)
    before = creature.get_creature_spawns_near(db, 0, *START, 150.0)
    assert len(before) >= 2
    hidden, kept = int(before[0]["guid"]), int(before[1]["guid"])
    con = sqlite3.connect(db)
    con.executemany("INSERT INTO creature_events(guid, event) VALUES(?, ?)",
                    [(hidden, 4), (kept, -4)])      # Darkmoon Faire on / off
    con.commit()
    con.close()
    after = {int(r["guid"]) for r in creature.get_creature_spawns_near(db, 0, *START, 150.0)}
    assert hidden not in after and kept in after
    assert len(after) == len(before) - 1
    assert hidden not in {int(r["guid"]) for r in creature.get_creature_spawns(db, 0)}


def test_build_event_rows_keeps_known_spawns_only():
    raw = [{"guid": "5", "event": "33"}, {"guid": "6", "event": "-4"},
           {"guid": "7", "event": "1"}, {"guid": "5", "event": "33"}]
    assert importer.build_event_rows(raw, [5, 6]) == [(5, 33), (6, -4)]
    assert importer.build_event_rows(None, [5]) == []
