"""Death -> ghost -> corpse reclaim flow."""
from __future__ import annotations

import asyncio
import sqlite3
import time

from src.common.config import AuthConfig, Config, DBConfig, LogConfig, WorldConfig
from src.protocol.opcodes import Opcodes
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


def test_corpse_create_block_uses_the_corpse_guid_and_fields():
    from src.world import object_update
    from src.world import update_fields as F

    corpse = {
        "owner_guid": 42, "map": 0, "x": 1.0, "y": 2.0, "z": 3.0, "o": 4.0,
        "display_id": 49, "items": [25] + [0] * 18,
        "bytes1": 0x04030201, "bytes2": 7, "guild": 9,
    }
    opcode, payload = object_update.build_corpse_update(corpse)
    if opcode == Opcodes.SMSG_COMPRESSED_UPDATE_OBJECT:
        import struct
        import zlib
        size = struct.unpack_from("<I", payload, 0)[0]
        payload = zlib.decompress(payload[4:])
        assert len(payload) == size
    offset = 0
    assert payload[offset] == 1                  # one block
    offset += 4
    assert payload[offset] == 3                  # UPDATETYPE_CREATE_OBJECT2
    offset += 1
    mask = payload[offset]
    offset += 1
    for bit in range(8):
        if mask & (1 << bit):
            offset += 1
    assert payload[offset] == 7                  # TYPEID_CORPSE
    offset += 1
    flags = int.from_bytes(payload[offset:offset + 2], "little")
    assert flags == 0x150
    offset += 2
    assert payload[offset] == 0                  # no transport
    offset += 1 + 8 * 4 + 4                      # position, duplicate, low guid
    blocks = payload[offset]
    offset += 1
    mask_bytes = payload[offset:offset + blocks * 4]
    offset += blocks * 4
    values = {}
    for index in range(blocks * 32):
        if mask_bytes[(index // 32) * 4 + (index % 32) // 8] \
                & (1 << (index % 8)):
            values[index] = int.from_bytes(payload[offset:offset + 4], "little")
            offset += 4
    assert values[F.OBJECT_FIELD_GUID] == 42 & 0xFFFFFFFF
    assert values[F.OBJECT_FIELD_GUID + 1] == object_update.corpse_guid(42) >> 32
    assert values[F.OBJECT_FIELD_TYPE] == (
        F.TYPEMASK_OBJECT | F.TYPEMASK_CORPSE)
    assert values[F.CORPSE_FIELD_OWNER] == 42
    assert values[F.CORPSE_FIELD_DISPLAY_ID] == 49
    assert values[F.CORPSE_FIELD_ITEM] == 25
    assert values[F.CORPSE_FIELD_BYTES_1] == 0x04030201
    assert values[F.CORPSE_FIELD_GUILD] == 9


def test_corpse_object_packs_appearance_and_item_fields(tmp_path):
    cfg, _ = _make_world(tmp_path)

    async def flow():
        socket = WorldSocket(asyncio.StreamReader(), FakeWriter(), cfg)
        socket.in_world = True
        char = {
            "race": 4, "gender": 1, "skin": 5, "face": 6, "hair_style": 7,
            "hair_color": 8, "facial_style": 9, "guild_id": 3,
            "map": 0, "x": 0.0, "y": 0.0, "z": 0.0, "o": 0.0,
            "inventory": {0: {"display_id": 1234, "inventory_type": 21}},
        }
        socket.character = char
        corpse = socket._corpse_object_data(char)
        # CMaNGOS corpse bytes are not the player appearance byte order.
        assert corpse["bytes1"] == (4 << 8) | (1 << 16) | (5 << 24)
        assert corpse["bytes2"] == 6 | (7 << 8) | (8 << 16) | (9 << 24)
        assert corpse["items"][0] == 1234 | (21 << 24)

    asyncio.run(flow())


def test_ghost_sees_its_own_corpse(tmp_path):
    cfg, guid = _make_world(tmp_path)
    db = cfg.database.world_path
    character_db.save_character_corpse(
        db, guid, 0, 12, -8935.0, -188.0, 80.0, 1.0)

    async def flow():
        writer = FakeWriter()
        socket = WorldSocket(asyncio.StreamReader(), writer, cfg)
        socket.in_world = True
        socket.player_guid = guid
        socket.character = dict(character_db.get_character(db, 1, guid))
        socket.character.update({"map": 0, "x": -8935.0, "y": -188.0,
                                 "z": 80.0})
        await socket.refresh_corpse_visibility(force=True)
        assert guid in socket._visible_corpses
        assert writer.writes

        # The broadcast path must also include the owner, not only nearby
        # players.
        writer.writes.clear()
        socket.server = type("Server", (), {"sessions": [socket]})()
        corpse = socket._corpse_object_data(socket.character)
        corpse.update({"character_guid": guid, "owner_guid": guid})
        await socket._broadcast_corpse_create(corpse)
        assert writer.writes

    asyncio.run(flow())


def test_release_and_resurrect_manage_ghost_auras(tmp_path):
    cfg, guid = _make_world(tmp_path)
    db = cfg.database.world_path

    async def flow():
        socket = WorldSocket(asyncio.StreamReader(), FakeWriter(), cfg)
        socket.in_world = True
        socket.player_guid = guid
        socket.character = dict(character_db.get_character(db, 1, guid))
        socket.character["race"] = 4          # night elf
        socket._apply_combat_stats()
        socket.character["health"] = 0
        combat = socket._get_combat_state()
        combat["dead"] = True

        await socket._record_death()
        await socket._release_spirit()
        spell_ids = {int(aura["spell_id"]) for aura in combat["auras"]}
        assert 8326 in spell_ids
        assert 20584 in spell_ids

        await socket._resurrect_player(at_corpse=True)
        spell_ids = {int(aura["spell_id"]) for aura in combat["auras"]}
        assert 8326 not in spell_ids
        assert 20584 not in spell_ids

    asyncio.run(flow())


def test_corpse_query_finds_appearance_and_equipment(tmp_path):
    cfg, guid = _make_world(tmp_path)
    db = cfg.database.world_path
    con = sqlite3.connect(db)
    try:
        con.execute(
            "INSERT INTO character_items(character_guid, item_entry, slot,"
            " stack_count) VALUES(?,?,?,?)", (guid, 25, 0, 1))
        con.commit()
    finally:
        con.close()
    character_db.save_character_corpse(
        db, guid, 0, 12, -8935.0, -188.0, 80.0, 1.0)
    template = character_db.get_item_template(db, 25)
    assert template is not None
    expected = (int(template["display_id"])
                | (int(template["inventory_type"]) << 24))
    rows = character_db.get_corpses_near(db, 0, -8935.0, -188.0, 80.0, 100)
    assert len(rows) == 1
    row = rows[0]
    assert row["character_guid"] == guid
    assert row["race"] == 1
    # CORPSE_FIELD_ITEM carries displayId | inventoryType << 24, not the entry.
    assert row["items"][0] == expected


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
        assert any(int(aura["spell_id"]) == 8326
                   for aura in combat["auras"])

    asyncio.run(flow())


def test_instance_corpse_reclaims_from_the_entrance_graveyard(tmp_path):
    cfg, guid = _make_world(tmp_path)
    db = cfg.database.world_path
    con = sqlite3.connect(db)
    try:
        con.execute(
            "INSERT INTO graveyard_locations(id, map, x, y, z, o, name)"
            " VALUES(4, 0, 100.0, 200.0, 10.0, 0.0, 'Instance Entrance')")
        con.execute(
            "INSERT INTO graveyard_zones(id, ghost_loc, link_kind, faction)"
            " VALUES(4, 999, 0, 469)")
        con.commit()
    finally:
        con.close()
    character_db.save_character_corpse(
        db, guid, 1, 999, 500.0, 600.0, 20.0, 1.0)
    character_db.mark_character_corpse_released(
        db, guid, int(time.time()) - 60)
    character_db.set_character_death_state(db, guid, 2)

    async def flow():
        socket = WorldSocket(asyncio.StreamReader(), FakeWriter(), cfg)
        socket.in_world = True
        socket.player_guid = guid
        socket.character = dict(character_db.get_character(db, 1, guid))
        socket._apply_combat_stats()
        socket.character.update({"map": 0, "x": 100.0, "y": 200.0,
                                 "z": 10.0, "death_state": 2})
        socket._get_combat_state()["dead"] = True
        socket._get_combat_state()["ghost"] = True
        await socket.handle_reclaim_corpse(b"")
        assert socket.character["map"] == 1
        assert socket.character["death_state"] == 0
        assert socket._get_combat_state()["dead"] is False
        assert getattr(socket, "_repop_pending", False) is True
        assert character_db.get_character_corpse(db, guid) is None

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
