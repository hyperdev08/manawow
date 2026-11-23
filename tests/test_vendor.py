"""Vendors: packet layouts, stock rules and a real client buying and selling."""
from __future__ import annotations

import asyncio
import os
import sqlite3
import struct

import pytest

from src.common.config import AuthConfig, Config, DBConfig, LogConfig, WorldConfig
from src.crypto.srp6 import SRP6
from src.protocol.opcodes import Opcodes
from src.world import character_db, object_update, vendor
from src.world import update_fields as F
from src.world.server import WorldServer
from src.auth.server import AuthServer
from tools.init_db import init_databases
from tools import protocol_selftest as st


# ----------------------------------------------------------------------
# Unit tests
# ----------------------------------------------------------------------
def _item(**kw):
    base = {"item": 159, "maxcount": 0, "incrtime": 0, "extended_cost": 0,
            "display_id": 18084, "buy_price": 25, "max_durability": 0,
            "buy_count": 5, "flags2": 0, "allowable_class": -1, "bonding": 0}
    base.update(kw)
    return base


def parse_list_inventory(payload: bytes):
    guid, count = struct.unpack_from("<QB", payload, 0)
    items = []
    for i in range(count):
        fields = struct.unpack_from("<IIIIIIII", payload, 9 + i * 32)
        items.append(dict(zip(
            ("slot", "item", "display", "left", "price", "durability",
             "buy_count", "extended_cost"), fields)))
    return guid, items


def test_list_inventory_layout():
    items = [_item(), _item(item=4540, maxcount=2, buy_count=1, buy_price=5)]
    payload = vendor.build_list_inventory(0xF13000012700044C, items, 1, 1,
                                          lambda it: 2 if it["maxcount"] else vendor.UNLIMITED)
    guid, parsed = parse_list_inventory(payload)
    assert guid == 0xF13000012700044C
    assert [p["slot"] for p in parsed] == [1, 2]          # 1-based
    assert parsed[0]["left"] == 0xFFFFFFFF                 # unlimited
    assert parsed[0]["buy_count"] == 5 and parsed[0]["price"] == 25
    assert parsed[1]["left"] == 2
    assert len(payload) == 9 + 2 * 32


def test_empty_list_has_error_byte():
    assert vendor.build_list_inventory(1, [], 1, 1, lambda _: 0) == struct.pack("<QBB", 1, 0, 0)


def test_hidden_items_keep_the_slot_numbers_of_the_rest():
    items = [_item(item=1, flags2=vendor.ITEM_FLAG2_HORDE_ONLY), _item(item=2)]
    _, parsed = parse_list_inventory(
        vendor.build_list_inventory(1, items, 1, 1, lambda _: vendor.UNLIMITED))
    assert [(p["slot"], p["item"]) for p in parsed] == [(2, 2)]


@pytest.mark.parametrize("item,race,cls,visible", [
    (_item(flags2=vendor.ITEM_FLAG2_HORDE_ONLY), 1, 1, False),     # human
    (_item(flags2=vendor.ITEM_FLAG2_HORDE_ONLY), 2, 1, True),      # orc
    (_item(flags2=vendor.ITEM_FLAG2_ALLIANCE_ONLY), 2, 1, False),
    (_item(bonding=1, allowable_class=1 << 7), 1, 1, False),       # BoP mage item, warrior
    (_item(bonding=1, allowable_class=1 << 7), 1, 8, True),        # ... mage
    (_item(bonding=2, allowable_class=1 << 7), 1, 1, True),        # BoE: anyone may buy
    (_item(extended_cost=2359), 1, 1, False),                      # honor items: not yet
])
def test_visibility(item, race, cls, visible):
    assert vendor.visible_to(item, race, cls) is visible


def test_limited_stock_sells_out_and_refills():
    now = [1000.0]
    stock = vendor.VendorStock(clock=lambda: now[0])
    item = _item(maxcount=2, incrtime=60)
    assert stock.remaining(7, item) == 2
    assert stock.take(7, item, 1) == 1
    assert stock.take(7, item, 1) == 0
    assert stock.remaining(8, item) == 2          # per spawn
    now[0] += 59
    assert stock.remaining(7, item) == 0
    now[0] += 1
    assert stock.remaining(7, item) == 2          # refilled after incrtime


def test_unlimited_stock():
    stock = vendor.VendorStock()
    assert stock.take(1, _item(), 100) == vendor.UNLIMITED


def test_buy_packets():
    assert vendor.build_buy_item(5, 0, vendor.UNLIMITED, 1) == struct.pack("<QIII", 5, 1, 0xFFFFFFFF, 1)
    assert vendor.build_buy_failed(5, 159, 2) == struct.pack("<QIB", 5, 159, 2)
    assert vendor.parse_buy_item(struct.pack("<QIIIB", 5, 159, 1, 2, 0)) == (5, 159, 1, 2)
    assert vendor.parse_sell_item(struct.pack("<QQI", 5, 9, 0)) == (5, 9, 0)


def test_stacks_only_merge_with_the_same_item(tmp_path):
    """Regression: looting/buying used to top up *any* partial stack."""
    db = str(tmp_path / "world.db")
    character_db.init_character_db(db)
    con = sqlite3.connect(db)
    con.executemany(
        "INSERT OR REPLACE INTO item_templates(entry, name1, stackable) VALUES(?,?,?)",
        [(159, "Water", 20), (4540, "Bread", 20)])
    con.commit()
    con.close()
    first = character_db.add_item_to_inventory(db, 1, 159, 5)
    second = character_db.add_item_to_inventory(db, 1, 4540, 3)
    assert first[0]["created"] and second[0]["created"]
    assert first[0]["slot"] != second[0]["slot"]
    third = character_db.add_item_to_inventory(db, 1, 159, 2)
    assert not third[0]["created"] and third[0]["slot"] == first[0]["slot"]
    assert third[0]["stack_count"] == 7


# ----------------------------------------------------------------------
# End to end: a simulated 3.3.5a client trades with two NPCs
# ----------------------------------------------------------------------
FARLEY = object_update.creature_guid(1003, 295)     # pure vendor
MICHELLE = object_update.creature_guid(1004, 2327)  # gossip menu + vendor
START = (-8949.95, -132.49, 83.53)                  # human warrior start
WATER, BREAD, SWORD, JUNK = 159, 4540, 2488, 117


def _prepare_world(world_db: str) -> None:
    con = sqlite3.connect(world_db)
    try:
        con.executemany(
            "INSERT OR REPLACE INTO item_templates(entry, name1, display_id, stackable, "
            "buy_price, sell_price, buy_count) VALUES(?,?,?,?,?,?,?)",
            [(WATER, "Refreshing Spring Water", 18084, 20, 25, 1, 5),
             (BREAD, "Tough Hunk of Bread", 6399, 20, 5, 1, 1),
             (SWORD, "Expensive Sword", 1542, 1, 100000, 0, 1),
             (JUNK, "No-value Junk", 1, 20, 1, 0, 1)])
        con.executemany(
            "INSERT INTO npc_vendor(entry, slot, item, maxcount, incrtime) VALUES(?,?,?,?,?)",
            [(295, 0, WATER, 0, 0), (295, 1, BREAD, 2, 3600), (295, 2, SWORD, 0, 0),
             (295, 3, JUNK, 0, 0), (2327, 0, WATER, 0, 0)])
        con.execute("UPDATE creature_templates SET npc_flags = 128 WHERE entry = 295")
        con.execute("UPDATE creature_templates SET npc_flags = 129 WHERE entry = 2327")
        # Stand both vendors next to the starting point.
        con.execute("UPDATE creature_spawns SET x = ?, y = ?, z = ? WHERE guid IN (1003, 1004)",
                    (START[0] + 2, START[1], START[2]))
        con.commit()
    finally:
        con.close()


async def _read_until(reader, crypt, wanted):
    seen = []
    while True:
        opcode, payload = await asyncio.wait_for(st.read_server_packet(reader, crypt), 5)
        seen.append(opcode)
        if opcode in wanted:
            return opcode, payload, seen


def _money(world_db: str, guid: int) -> int:
    con = sqlite3.connect(world_db)
    try:
        return int(con.execute("SELECT money FROM characters WHERE guid = ?", (guid,)).fetchone()[0])
    finally:
        con.close()


def _stacks(world_db: str, guid: int, entry: int):
    con = sqlite3.connect(world_db)
    try:
        return [(int(r[0]), int(r[1])) for r in con.execute(
            "SELECT id, stack_count FROM character_items WHERE character_guid = ? "
            "AND item_entry = ? ORDER BY slot", (guid, entry))]
    finally:
        con.close()


async def _vendor_flow(tmp_path) -> None:
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
    _prepare_world(world_db)
    salt, verifier = SRP6.make_registration_data(st.USERNAME, st.PASSWORD)
    con = sqlite3.connect(auth_db)
    con.execute("INSERT INTO account(username, v, s, expansion) VALUES(?,?,?,2)",
                (st.USERNAME, verifier, salt))
    con.commit()
    con.close()
    vendor.STOCK.clear()

    world = await asyncio.start_server(WorldServer(cfg).handle_client, "127.0.0.1", 0)
    cfg.worldserver.port = world.sockets[0].getsockname()[1]
    auth = await asyncio.start_server(AuthServer(cfg).handle_client, "127.0.0.1", 0)
    cfg.authserver.port = auth.sockets[0].getsockname()[1]
    writer = None
    try:
        session_key, _addr, realm_id = await st.authserver_flow("127.0.0.1", cfg.authserver.port)
        reader, writer, crypt = await st.world_handshake(
            "127.0.0.1", cfg.worldserver.port, st.USERNAME, session_key, realm_id)

        create = b"Buyer\x00" + bytes([1, 1, 0, 0, 0, 0, 0, 0, 0])
        await st.send_client_packet(writer, int(Opcodes.CMSG_CHAR_CREATE), create, crypt)
        await st.read_server_packet(reader, crypt)
        await st.send_client_packet(writer, int(Opcodes.CMSG_CHAR_ENUM), crypt=crypt)
        _op, enum_payload = await st.read_server_packet(reader, crypt)
        player = struct.unpack_from("<Q", enum_payload, 1)[0]
        con = sqlite3.connect(world_db)
        con.execute("UPDATE characters SET money = 1000 WHERE guid = ?", (player,))
        con.commit()
        con.close()
        await st.send_client_packet(writer, int(Opcodes.CMSG_PLAYER_LOGIN),
                                    struct.pack("<Q", player), crypt)
        await st.read_login_burst(reader, crypt)

        async def send(opcode, payload):
            await st.send_client_packet(writer, int(opcode), payload, crypt)

        # -- list --------------------------------------------------------
        await send(Opcodes.CMSG_LIST_INVENTORY, struct.pack("<Q", FARLEY))
        _op, payload, _ = await _read_until(reader, crypt, {Opcodes.SMSG_LIST_INVENTORY})
        guid, items = parse_list_inventory(payload)
        assert guid == FARLEY
        assert [(i["slot"], i["item"]) for i in items] == [(1, WATER), (2, BREAD), (3, SWORD), (4, JUNK)]
        assert items[0]["buy_count"] == 5 and items[1]["left"] == 2

        async def buy(item, slot, count=1):
            await send(Opcodes.CMSG_BUY_ITEM, struct.pack("<QIIIB", FARLEY, item, slot, count, 0))
            return await _read_until(reader, crypt, {Opcodes.SMSG_BUY_ITEM, Opcodes.SMSG_BUY_FAILED,
                                                     Opcodes.SMSG_INVENTORY_CHANGE_FAILURE})

        # -- buy one bundle of 5 water for 25c ---------------------------
        op, payload, seen = await buy(WATER, 1)
        assert op == Opcodes.SMSG_BUY_ITEM, seen
        assert struct.unpack("<QIII", payload) == (FARLEY, 1, 0xFFFFFFFF, 1)
        assert Opcodes.SMSG_ITEM_PUSH_RESULT in seen
        assert _money(world_db, player) == 975
        assert [c for _, c in _stacks(world_db, player, WATER)] == [5]

        # -- limited stock: 2 bread --------------------------------------
        op, payload, _ = await buy(BREAD, 2, 3)
        assert op == Opcodes.SMSG_BUY_FAILED and payload[-1] == vendor.BUY_ERR_ITEM_ALREADY_SOLD
        op, payload, _ = await buy(BREAD, 2, 2)
        assert op == Opcodes.SMSG_BUY_ITEM and struct.unpack("<QIII", payload)[2] == 0
        op, payload, _ = await buy(BREAD, 2, 1)
        assert op == Opcodes.SMSG_BUY_FAILED and payload[-1] == vendor.BUY_ERR_ITEM_SOLD_OUT
        assert _money(world_db, player) == 965
        assert [c for _, c in _stacks(world_db, player, BREAD)] == [2]  # not merged into water

        # -- errors ------------------------------------------------------
        op, payload, _ = await buy(SWORD, 3)
        assert op == Opcodes.SMSG_BUY_FAILED and payload[-1] == vendor.BUY_ERR_NOT_ENOUGHT_MONEY
        op, payload, _ = await buy(WATER, 2)                 # wrong slot for that item
        assert op == Opcodes.SMSG_BUY_FAILED and payload[-1] == vendor.BUY_ERR_CANT_FIND_ITEM

        # -- sell 2 of the 5 water (1c each) -----------------------------
        water_id, _ = _stacks(world_db, player, WATER)[0]
        water_guid = F.ITEM_HIGH_GUID | water_id
        await send(Opcodes.CMSG_SELL_ITEM, struct.pack("<QQI", FARLEY, water_guid, 2))
        await send(Opcodes.CMSG_PING, struct.pack("<II", 1, 0))
        _op, _payload, seen = await _read_until(reader, crypt, {Opcodes.SMSG_PONG})
        assert Opcodes.SMSG_SELL_ITEM not in seen           # success sends no SELL_ITEM
        assert _money(world_db, player) == 967
        assert [c for _, c in _stacks(world_db, player, WATER)] == [3]

        # -- selling a zero-value item fails -----------------------------
        op, payload, _ = await buy(JUNK, 4)
        assert op == Opcodes.SMSG_BUY_ITEM
        junk_id, _ = _stacks(world_db, player, JUNK)[0]
        await send(Opcodes.CMSG_SELL_ITEM, struct.pack("<QQI", FARLEY, F.ITEM_HIGH_GUID | junk_id, 0))
        _op, payload, _ = await _read_until(reader, crypt, {Opcodes.SMSG_SELL_ITEM})
        assert payload[-1] == vendor.SELL_ERR_CANT_SELL_ITEM

        # -- gossip vendor: menu gets a "browse goods" option ------------
        await send(Opcodes.CMSG_GOSSIP_HELLO, struct.pack("<Q", MICHELLE))
        _op, payload, _ = await _read_until(reader, crypt, {Opcodes.SMSG_GOSSIP_MESSAGE})
        gossip = st.parse_gossip_message(payload)
        browse = [o for o in gossip["options"] if o["index"] == vendor.VENDOR_GOSSIP_OPTION_INDEX]
        assert browse and browse[0]["type"] == vendor.GOSSIP_ICON_VENDOR  # icon byte
        await send(Opcodes.CMSG_GOSSIP_SELECT_OPTION,
                   struct.pack("<QII", MICHELLE, gossip["gossip_id"], vendor.VENDOR_GOSSIP_OPTION_INDEX))
        _op, payload, seen = await _read_until(reader, crypt, {Opcodes.SMSG_LIST_INVENTORY})
        assert Opcodes.SMSG_GOSSIP_COMPLETE in seen
        assert [i["item"] for i in parse_list_inventory(payload)[1]] == [WATER]
    finally:
        if writer is not None:
            writer.close()
        world.close()
        auth.close()
        await world.wait_closed()
        await auth.wait_closed()


def test_vendor_end_to_end(tmp_path):
    asyncio.run(_vendor_flow(tmp_path))
