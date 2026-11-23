"""End-to-end: a real client session takes and turns in a quest over the wire."""
from __future__ import annotations

import asyncio
import os
import sqlite3
import struct
import tempfile

import pytest

from src.auth.server import AuthServer
from src.common.config import AuthConfig, Config, DBConfig, LogConfig, WorldConfig
from src.crypto.srp6 import SRP6
from src.protocol.opcodes import Opcodes
from src.world import creature
from src.world import quests as Q
from src.world.server import WorldServer
from tools.init_db import init_databases
from tools.protocol_selftest import (
    BUILD,
    authserver_flow,
    create_and_enter,
    read_server_packet,
    send_client_packet,
)

USERNAME = "QUESTER"
PASSWORD = "QUESTER"
MCBRIDE_ENTRY = 197
MCBRIDE_LOW_GUID = 1001          # demo Northshire population in a fresh DB


def _config(temp_dir: str) -> Config:
    return Config(
        authserver=AuthConfig(
            host="127.0.0.1", port=0, realm_id=1, realm_name="QuestTest",
            realm_address="127.0.0.1", realm_type=0, realm_flags=0,
            realm_population=0.0, realm_timezone=1,
            realm_build_min=BUILD, realm_build_max=BUILD,
        ),
        worldserver=WorldConfig(host="127.0.0.1", port=0, realm_id=1, motd="QuestTest"),
        database=DBConfig(auth_path=os.path.join(temp_dir, "auth.db"),
                          world_path=os.path.join(temp_dir, "world.db")),
        logging=LogConfig(level="WARNING", log_dir=os.path.join(temp_dir, "logs"),
                          console=False),
    )


def _seed(cfg: Config) -> None:
    salt, verifier = SRP6.make_registration_data(USERNAME, PASSWORD)
    con = sqlite3.connect(cfg.database.auth_path)
    try:
        con.execute("INSERT INTO account(username, v, s, expansion) VALUES(?,?,?,2)",
                    (USERNAME, verifier, salt))
        con.commit()
    finally:
        con.close()

    row = {column: 0 for column in Q.QUEST_COLUMNS}
    for name in (Q.column_name(c) for c in Q.TEXT_COLUMNS):
        row[name] = ""
    row.update(entry=783, method=2, min_level=1, quest_level=1,
               title="A Threat Within", details="Talk to Deputy Willem.",
               objectives="Speak with the Marshal.", rew_money_max_level=30)
    con = sqlite3.connect(cfg.database.world_path)
    try:
        columns = Q.QUEST_COLUMNS
        con.execute(
            f"INSERT INTO quest_templates({', '.join(columns)}) "
            f"VALUES({', '.join('?' for _ in columns)})", [row[c] for c in columns])
        con.execute("INSERT INTO creature_quest_starters VALUES(?, 783)", (MCBRIDE_ENTRY,))
        con.execute("INSERT INTO creature_quest_enders VALUES(?, 783)", (MCBRIDE_ENTRY,))
        # Like the imported CMaNGOS row: quest giver flag and a menu without
        # options (the demo seed gives him a gossip menu with options).
        con.execute("UPDATE creature_templates SET npc_flags = npc_flags | 2, "
                    "gossip_menu_id = 0 WHERE entry = ?", (MCBRIDE_ENTRY,))
        con.commit()
    finally:
        con.close()


async def _read_until(reader, crypt, opcode, limit: int = 200):
    for _ in range(limit):
        got, payload = await asyncio.wait_for(read_server_packet(reader, crypt), 300)
        if got == int(opcode):
            return payload
    raise AssertionError(f"0x{int(opcode):04X} not received")


async def _flow() -> None:
    temp_dir = tempfile.mkdtemp(prefix="manawow-quest-e2e-")
    cfg = _config(temp_dir)
    init_databases(cfg)
    _seed(cfg)

    auth_server = AuthServer(cfg)
    world_server = WorldServer(cfg)
    world_listener = await asyncio.start_server(world_server.handle_client, "127.0.0.1", 0)
    cfg.worldserver.port = world_listener.sockets[0].getsockname()[1]
    auth_listener = await asyncio.start_server(auth_server.handle_client, "127.0.0.1", 0)
    cfg.authserver.port = auth_listener.sockets[0].getsockname()[1]
    try:
        session_key, _addr, realm_id = await authserver_flow(
            "127.0.0.1", cfg.authserver.port, USERNAME, PASSWORD)
        reader, writer, crypt, _guid = await create_and_enter(
            "127.0.0.1", cfg.worldserver.port, USERNAME, session_key, realm_id, "Questor")
        giver = creature.creature_guid(MCBRIDE_LOW_GUID, MCBRIDE_ENTRY)

        async def send(opcode, payload=b""):
            await send_client_packet(writer, int(opcode), payload, crypt)

        await send(Opcodes.CMSG_QUESTGIVER_STATUS_QUERY, struct.pack("<Q", giver))
        status = await _read_until(reader, crypt, Opcodes.SMSG_QUESTGIVER_STATUS)
        assert struct.unpack("<QB", status) == (giver, Q.DIALOG_STATUS_AVAILABLE)

        # One quest and no gossip options: the details window opens directly.
        await send(Opcodes.CMSG_GOSSIP_HELLO, struct.pack("<Q", giver))
        details = await _read_until(reader, crypt, Opcodes.SMSG_QUESTGIVER_QUEST_DETAILS)
        assert struct.unpack_from("<QQI", details, 0) == (giver, 0, 783)

        await send(Opcodes.CMSG_QUEST_QUERY, struct.pack("<I", 783))
        response = await _read_until(reader, crypt, Opcodes.SMSG_QUEST_QUERY_RESPONSE)
        assert struct.unpack_from("<I", response, 0)[0] == 783

        await send(Opcodes.CMSG_QUESTGIVER_ACCEPT_QUEST, struct.pack("<QII", giver, 783, 0))
        done = await _read_until(reader, crypt, Opcodes.SMSG_QUESTUPDATE_COMPLETE)
        assert struct.unpack("<I", done)[0] == 783
        status = await _read_until(reader, crypt, Opcodes.SMSG_QUESTGIVER_STATUS)
        assert struct.unpack("<QB", status) == (giver, Q.DIALOG_STATUS_REWARD)

        await send(Opcodes.CMSG_QUESTGIVER_COMPLETE_QUEST, struct.pack("<QI", giver, 783))
        offer = await _read_until(reader, crypt, Opcodes.SMSG_QUESTGIVER_OFFER_REWARD)
        assert struct.unpack_from("<QI", offer, 0) == (giver, 783)

        await send(Opcodes.CMSG_QUESTGIVER_CHOOSE_REWARD, struct.pack("<QII", giver, 783, 0))
        complete = await _read_until(reader, crypt, Opcodes.SMSG_QUESTGIVER_QUEST_COMPLETE)
        assert struct.unpack_from("<II", complete, 0) == (783, 50)

        writer.close()
    finally:
        auth_listener.close()
        world_listener.close()
        await auth_listener.wait_closed()
        await world_listener.wait_closed()

    assert 783 in Q.load_rewarded_quests(cfg.database.world_path, _guid)


@pytest.mark.slow
def test_quest_accept_and_turn_in_over_the_wire():
    asyncio.run(_flow())
