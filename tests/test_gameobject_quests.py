"""Quests on game objects and items: posters, goobers, use objectives, quest items."""
from __future__ import annotations

import asyncio
import sqlite3
import struct

from src.auth.server import AuthServer
from src.common.config import AuthConfig, Config, DBConfig, LogConfig, WorldConfig
from src.crypto.srp6 import SRP6
from src.protocol.opcodes import Opcodes
from src.world import character_db
from src.world import gameobject as go
from src.world import quests as Q
from src.world import update_fields as F
from src.world.server import WorldServer
from tools import import_wotlk_db as importer
from tools import protocol_selftest as st
from tools.init_db import init_databases
from tests.test_gameobject import START, _insert, _spawn, _template
from tests.test_gameobject_use import _read_until, _values_update

POSTER, GOOBER, SPARKLE_ROCK, HIGH_POSTER = 801, 802, 803, 804
HIGH_QUEST = 90003    # needs level 10
QUEST = 90001          # started and ended by the poster, "use the goober" objective
ITEM_QUEST = 90002     # started by an item
QUEST_ITEM = 1307      # Gold Pickup Schedule


def _guid(low, entry):
    return go.gameobject_guid(low, entry)


# ----------------------------------------------------------------------
# Unit tests
# ----------------------------------------------------------------------
def _quest_db(tmp_path) -> str:
    db = str(tmp_path / "world.db")
    from src.world import creature
    creature.init_creature_db(db)
    character_db.init_character_db(db)
    Q.init_quest_db(db)
    go.init_gameobject_db(db)
    return db


def test_relations_are_kept_apart_per_giver_kind(tmp_path):
    db = _quest_db(tmp_path)
    con = sqlite3.connect(db)
    con.execute("INSERT INTO creature_quest_starters(entry, quest) VALUES(55, 1)")
    con.execute("INSERT INTO gameobject_quest_starters(entry, quest) VALUES(55, 2)")
    con.execute("INSERT INTO gameobject_quest_enders(entry, quest) VALUES(55, 3)")
    con.execute("INSERT OR REPLACE INTO item_templates(entry, name1, start_quest) "
                "VALUES(55, 'Note', 4)")
    con.commit()
    con.close()
    Q.clear_cache()
    assert Q.quests_started_by(db, 55) == (1,)
    assert Q.quests_started_by(db, 55, Q.GIVER_GAMEOBJECT) == (2,)
    assert Q.quests_ended_by(db, 55, Q.GIVER_GAMEOBJECT) == (3,)
    assert Q.quests_started_by(db, 55, Q.GIVER_ITEM) == (4,)
    assert Q.quests_ended_by(db, 55, Q.GIVER_ITEM) == ()
    assert Q.is_quest_giver(db, 55, Q.GIVER_GAMEOBJECT)
    assert not Q.is_quest_giver(db, 56, Q.GIVER_GAMEOBJECT)
    Q.clear_cache()


def test_object_objectives_are_the_negative_ids():
    quest = {"req_creature_or_go_id1": 299, "req_creature_or_go_count1": 8,
             "req_creature_or_go_id2": -802, "req_creature_or_go_count2": 3}
    assert Q.object_objectives(quest) == [(1, 802, 3)]


def test_goober_fields():
    goober = _template(type=go.GAMEOBJECT_TYPE_GOOBER, data1=QUEST, data3=4000, data4=0,
                       data5=1, data7=12, data10=555)
    assert go.object_quest_id(goober) == QUEST
    assert go.goober_page(goober) == 12 and go.goober_spell(goober) == 555
    assert go.goober_consumable(goober) and go.goober_reset_seconds(goober) == 4.0
    assert go.goober_reset_seconds(_template(type=go.GAMEOBJECT_TYPE_GOOBER)) == 1.0
    generic = _template(type=go.GAMEOBJECT_TYPE_GENERIC, data5=QUEST)
    assert go.object_quest_id(generic) == QUEST
    assert go.is_quest_object(generic, "unused") and go.is_quest_object(goober, "unused")
    assert not go.is_quest_object(_template(type=go.GAMEOBJECT_TYPE_GOOBER), "unused")


def test_importer_knows_the_object_relations():
    tables = [t for _dump, t, _key in importer.QUEST_RELATION_TABLES]
    assert "gameobject_quest_starters" in tables and "gameobject_quest_enders" in tables


# ----------------------------------------------------------------------
# End to end
# ----------------------------------------------------------------------
def _prepare(world_db: str) -> None:
    x, y, z = START
    con = sqlite3.connect(world_db)
    _insert(con,
            [_template(entry=POSTER, type=go.GAMEOBJECT_TYPE_QUESTGIVER, name="Wanted Poster"),
             _template(entry=GOOBER, type=go.GAMEOBJECT_TYPE_GOOBER, name="Suspicious Crate",
                       data1=QUEST),
             _template(entry=SPARKLE_ROCK, type=go.GAMEOBJECT_TYPE_GENERIC, name="Odd Rock",
                       data5=QUEST),
             _template(entry=HIGH_POSTER, type=go.GAMEOBJECT_TYPE_QUESTGIVER,
                       name="Wanted: Hogger", flags=4)],
            [_spawn(guid=21, entry=POSTER, x=x + 2, y=y, z=z,
                    type=go.GAMEOBJECT_TYPE_QUESTGIVER),
             _spawn(guid=22, entry=GOOBER, x=x - 2, y=y, z=z, type=go.GAMEOBJECT_TYPE_GOOBER),
             _spawn(guid=23, entry=SPARKLE_ROCK, x=x, y=y + 2, z=z,
                    type=go.GAMEOBJECT_TYPE_GENERIC),
             _spawn(guid=24, entry=HIGH_POSTER, x=x, y=y - 2, z=z, flags=4,
                    type=go.GAMEOBJECT_TYPE_QUESTGIVER)])
    con.executemany("INSERT INTO gameobject_quest_starters(entry, quest) VALUES(?, ?)",
                    [(POSTER, QUEST), (HIGH_POSTER, HIGH_QUEST)])
    con.execute("INSERT OR REPLACE INTO quest_templates(entry, title, min_level, quest_level) "
                "VALUES(?, 'Wanted: Hogger', 10, 11)", (HIGH_QUEST,))
    con.executemany("INSERT INTO gameobject_quest_enders(entry, quest) VALUES(?, ?)",
                    [(POSTER, QUEST)])
    con.execute("INSERT OR REPLACE INTO quest_templates(entry, title, min_level, quest_level, "
                "req_creature_or_go_id1, req_creature_or_go_count1) "
                "VALUES(?, 'Inspect the Crate', 1, 1, ?, 1)", (QUEST, -GOOBER))
    con.execute("INSERT OR REPLACE INTO quest_templates(entry, title, min_level, quest_level) "
                "VALUES(?, 'The Collector', 1, 1)", (ITEM_QUEST,))
    con.execute("INSERT OR REPLACE INTO item_templates(entry, name1, display_id, stackable, "
                "start_quest) VALUES(?, 'Gold Pickup Schedule', 7695, 1, ?)",
                (QUEST_ITEM, ITEM_QUEST))
    con.commit()
    con.close()


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
    Q.clear_cache()
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
        create = b"Quester\x00" + bytes([1, 1, 0, 0, 0, 0, 0, 0, 0])
        await st.send_client_packet(writer, int(Opcodes.CMSG_CHAR_CREATE), create, crypt)
        await st.read_server_packet(reader, crypt)
        await st.send_client_packet(writer, int(Opcodes.CMSG_CHAR_ENUM), crypt=crypt)
        _op, enum_payload = await st.read_server_packet(reader, crypt)
        player = struct.unpack_from("<Q", enum_payload, 1)[0]
        assert character_db.add_item_to_inventory(world_db, player, QUEST_ITEM, 1)
        con = sqlite3.connect(world_db)
        item_id = con.execute("SELECT id FROM character_items WHERE character_guid = ? "
                              "AND item_entry = ?", (player, QUEST_ITEM)).fetchone()[0]
        con.close()
        item_guid = F.ITEM_HIGH_GUID | item_id
        await st.send_client_packet(writer, int(Opcodes.CMSG_PLAYER_LOGIN),
                                    struct.pack("<Q", player), crypt)
        burst = await st.read_login_burst(reader, crypt)
        objects = {b["guid"]: b for b in burst["gameobjects"]}
        goober_guid, rock_guid, poster_guid = (_guid(22, GOOBER), _guid(23, SPARKLE_ROCK),
                                               _guid(21, POSTER))
        assert objects[goober_guid]["values"].get(F.GAMEOBJECT_DYNAMIC, 0) == 0
        # A poster is clickable only while it has something for the player.
        poster_values = objects[_guid(21, POSTER)]["values"]
        assert poster_values[F.GAMEOBJECT_DYNAMIC] == go.GO_DYNFLAG_LO_ACTIVATE
        assert objects[_guid(24, HIGH_POSTER)]["values"].get(F.GAMEOBJECT_DYNAMIC, 0) == 0

        async def send(opcode, payload=b""):
            await st.send_client_packet(writer, int(opcode), payload, crypt)

        # -- the poster shows "!" and offers its quest ----------------
        await send(Opcodes.CMSG_QUESTGIVER_STATUS_MULTIPLE_QUERY)
        _op, payload, _ = await _read_until(reader, crypt,
                                            {Opcodes.SMSG_QUESTGIVER_STATUS_MULTIPLE})
        count = struct.unpack_from("<I", payload)[0]
        statuses = dict(struct.unpack_from("<QB", payload, 4 + i * 9) for i in range(count))
        assert statuses[poster_guid] == Q.DIALOG_STATUS_AVAILABLE

        # The goober is inert before the quest.
        await send(Opcodes.CMSG_GAMEOBJ_USE, struct.pack("<Q", goober_guid))
        await send(Opcodes.CMSG_GAMEOBJ_USE, struct.pack("<Q", poster_guid))
        _op, payload, seen = await _read_until(reader, crypt,
                                               {Opcodes.SMSG_QUESTGIVER_QUEST_DETAILS})
        assert struct.unpack_from("<Q", payload)[0] == poster_guid
        assert Opcodes.SMSG_QUESTUPDATE_ADD_KILL not in [op for op, _ in seen]

        await send(Opcodes.CMSG_QUESTGIVER_ACCEPT_QUEST, struct.pack("<QII", poster_guid, QUEST, 0))
        # Accepting lights up the goober (ACTIVATE|SPARKLE) and the rock (SPARKLE).
        flags = {}
        while len(flags) < 2:
            _op, payload, _ = await _read_until(
                reader, crypt, {Opcodes.SMSG_UPDATE_OBJECT, Opcodes.SMSG_COMPRESSED_UPDATE_OBJECT})
            if payload[4] != 0:
                continue
            guid, values = _values_update(payload)
            if F.GAMEOBJECT_DYNAMIC in values:
                flags[guid] = values[F.GAMEOBJECT_DYNAMIC]
        assert flags[goober_guid] == go.GO_DYNFLAG_LO_ACTIVATE | go.GO_DYNFLAG_LO_SPARKLE
        assert flags[rock_guid] == go.GO_DYNFLAG_LO_SPARKLE

        # -- using the goober credits the objective --------------------
        await send(Opcodes.CMSG_GAMEOBJ_USE, struct.pack("<Q", goober_guid))
        _op, payload, _ = await _read_until(reader, crypt, {Opcodes.SMSG_QUESTUPDATE_ADD_KILL})
        quest_id, target, count_now, required, credited = struct.unpack("<IiIIQ", payload)
        assert (quest_id, target, count_now, required, credited) == (
            QUEST, -GOOBER, 1, 1, goober_guid)
        await _read_until(reader, crypt, {Opcodes.SMSG_QUESTUPDATE_COMPLETE})
        session = next(s for s in server.sessions if s.player_guid == player)
        assert session._quest_log_map()[QUEST]["status"] == Q.QUEST_STATUS_COMPLETE
        assert server.gameobjects.state(22) == go.GO_STATE_ACTIVE

        # -- and the poster takes the quest back -----------------------
        await send(Opcodes.CMSG_QUESTGIVER_STATUS_QUERY, struct.pack("<Q", poster_guid))
        _op, payload, _ = await _read_until(reader, crypt, {Opcodes.SMSG_QUESTGIVER_STATUS})
        assert struct.unpack("<QB", payload) == (poster_guid, Q.DIALOG_STATUS_REWARD)
        await send(Opcodes.CMSG_QUESTGIVER_COMPLETE_QUEST, struct.pack("<QI", poster_guid, QUEST))
        await _read_until(reader, crypt, {Opcodes.SMSG_QUESTGIVER_OFFER_REWARD})
        await send(Opcodes.CMSG_QUESTGIVER_CHOOSE_REWARD,
                   struct.pack("<QII", poster_guid, QUEST, 0))
        await _read_until(reader, crypt, {Opcodes.SMSG_QUESTGIVER_QUEST_COMPLETE})
        assert QUEST in session._quest_state().rewarded

        # -- an item that starts a quest -------------------------------
        use = struct.pack("<BBBIQIB", 255, 23, 1, 0, item_guid, 0, 0) + struct.pack("<I", 0)
        await send(Opcodes.CMSG_USE_ITEM, use)
        _op, payload, _ = await _read_until(reader, crypt, {Opcodes.SMSG_QUESTGIVER_QUEST_DETAILS})
        assert struct.unpack_from("<QQI", payload)[0] == item_guid
        await send(Opcodes.CMSG_QUESTGIVER_ACCEPT_QUEST,
                   struct.pack("<QII", item_guid, ITEM_QUEST, 0))
        await _read_until(reader, crypt, {Opcodes.SMSG_GOSSIP_COMPLETE})
        assert ITEM_QUEST in session._quest_log_map()
        con = sqlite3.connect(world_db)
        left = con.execute("SELECT COUNT(*) FROM character_items WHERE character_guid = ? "
                           "AND item_entry = ?", (player, QUEST_ITEM)).fetchone()[0]
        con.close()
        assert left == 0                     # the item is used up
    finally:
        if writer is not None:
            writer.close()
        world.close()
        auth.close()
        await world.wait_closed()
        await auth.wait_closed()
        go.clear_cache()
        Q.clear_cache()


def test_posters_goobers_and_quest_items(tmp_path):
    asyncio.run(_flow(tmp_path))
