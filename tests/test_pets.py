"""Pet action bar, commands, autocast toggles and rename."""
from __future__ import annotations

import asyncio
import sqlite3
import struct

from src.common.config import AuthConfig, Config, DBConfig, LogConfig, WorldConfig
from src.protocol.opcodes import Opcodes
from src.world import creature, object_update
from src.world import update_fields as F
from src.world.realmsocket import WorldSocket
from src.world.worldsocket import pets


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


def _world(tmp_path):
    db = str(tmp_path / "world.db")
    creature.init_creature_db(db)
    con = sqlite3.connect(db)
    try:
        con.execute(
            "INSERT OR REPLACE INTO creature_templates"
            "(entry, name, display_id1, min_level, max_level, base_health,"
            " hp_multiplier, family, type, unit_class, unit_power_type,"
            " scale, bounding_radius, combat_reach, speed_walk, speed_run,"
            " melee_attack_speed)"
            " VALUES(416, 'Imp', 1166, 1, 1, 100, 1.0, 23, 3, 8, 0,"
            " 1.0, 0.389, 1.5, 2.5, 8.0, 2000)")
        con.execute(
            "INSERT OR REPLACE INTO creature_template_spells"
            "(entry, set_id, spell1) VALUES(416, 0, 3110)")
        con.execute(
            "INSERT OR REPLACE INTO spell_templates(id, name, spell_level)"
            " VALUES(3110, 'Firebolt', 1)")
        con.commit()
    finally:
        con.close()
    return db


def _socket(tmp_path):
    db = _world(tmp_path)
    cfg = Config(
        authserver=AuthConfig(), worldserver=WorldConfig(client_data=""),
        database=DBConfig(auth_path=str(tmp_path / "auth.db"), world_path=db),
        logging=LogConfig(level="WARNING", log_dir=str(tmp_path / "logs"),
                          console=False),
    )
    sent = []
    peer_sent = []

    async def send_packet(opcode, payload=b""):
        sent.append((int(opcode), bytes(payload)))

    async def send_packet_to_peers(opcode, payload=b""):
        peer_sent.append((int(opcode), bytes(payload)))

    socket = WorldSocket(asyncio.StreamReader(), FakeWriter(), cfg)
    socket.send_packet = send_packet
    socket.send_packet_to_peers = send_packet_to_peers
    socket.in_world = True
    socket.player_guid = 0x42
    socket.character = {
        "guid": 0x42, "name": "Tester", "class": 9, "race": 1, "level": 3,
        "map": 0, "zone": 12, "x": 0.0, "y": 0.0, "z": 0.0, "o": 0.0,
        "health": 100, "max_health": 100, "power": 100, "max_power": 100,
    }
    return socket, sent, peer_sent


def _parse_pet_spells(payload):
    guid, family, duration, react, command, flags = struct.unpack_from(
        "<QHIBBH", payload, 0)
    offset = struct.calcsize("<QHIBBH")
    bar = list(struct.unpack_from("<10I", payload, offset))
    offset += 40
    count = payload[offset]
    offset += 1
    spell_buttons = list(struct.unpack_from(f"<{count}I", payload, offset))
    offset += count * 4
    cooldowns = payload[offset]
    return {
        "guid": guid, "family": family, "duration": duration,
        "react": react, "command": command, "flags": flags,
        "bar": bar, "spells": spell_buttons, "cooldowns": cooldowns,
    }


def test_summon_sends_pet_action_bar(tmp_path):
    async def flow():
        socket, sent, _peer_sent = _socket(tmp_path)
        low = await socket._summon_creature(416, -1, is_pet=True)
        assert low is not None
        spawn = socket._summoned_creatures[low]
        state = socket._summon_state(low)
        values = object_update.build_creature_values(spawn)
        assert values[F.UNIT_FIELD_PETNUMBER] == low
        assert values[F.UNIT_FIELD_BYTES_2] == (
            pets.UNIT_PET_FLAG_CAN_BE_RENAMED
            | pets.UNIT_PET_FLAG_CAN_BE_ABANDONED) << 16

        packet = next(payload for opcode, payload in sent
                      if opcode == int(Opcodes.SMSG_PET_SPELLS))
        parsed = _parse_pet_spells(packet)
        assert parsed["guid"] == creature.creature_guid(low, 416)
        assert parsed["family"] == 23
        assert parsed["duration"] == 0
        assert parsed["react"] == pets.REACT_DEFENSIVE
        assert parsed["command"] == pets.COMMAND_FOLLOW
        assert parsed["flags"] == 0
        assert parsed["bar"][0] == pets.pack_action(
            pets.COMMAND_ATTACK, pets.ACT_COMMAND)
        assert parsed["bar"][1] == pets.pack_action(
            pets.COMMAND_FOLLOW, pets.ACT_COMMAND)
        assert parsed["bar"][2] == pets.pack_action(
            pets.COMMAND_STAY, pets.ACT_COMMAND)
        assert parsed["bar"][3] == pets.pack_action(
            3110, pets.ACT_DISABLED)
        assert parsed["bar"][7] == pets.pack_action(
            pets.REACT_AGGRESSIVE, pets.ACT_REACTION)
        assert parsed["bar"][8] == pets.pack_action(
            pets.REACT_DEFENSIVE, pets.ACT_REACTION)
        assert parsed["bar"][9] == pets.pack_action(
            pets.REACT_PASSIVE, pets.ACT_REACTION)
        assert parsed["spells"] == [pets.pack_action(
            3110, pets.ACT_DISABLED)]
        assert parsed["cooldowns"] == 0
        assert state["pet_number"] == low

    asyncio.run(flow())


def test_pet_commands_and_reactions(tmp_path):
    async def flow():
        socket, sent, _peer_sent = _socket(tmp_path)
        low = await socket._summon_creature(416, -1, is_pet=True)
        spawn = socket._summoned_creatures[low]
        state = socket._summon_state(low)
        pet_guid = creature.creature_guid(low, 416)
        enemy_guid = creature.creature_guid(7, 1009)
        enemy = {
            "guid": 7, "entry": 1009, "name": "Enemy", "x": 4.0, "y": 0.0,
            "z": 0.0, "o": 0.0, "dead_since": 0, "combat_reach": 1.5,
            "min_level": 20, "max_level": 20, "base_health": 500,
            "hp_multiplier": 1.0,
        }
        original = socket._get_creature_spawn
        socket._get_creature_spawn = lambda value: (
            enemy if int(value) == 7 else original(value))

        await socket.handle_pet_action(struct.pack(
            "<QIQ", pet_guid,
            pets.pack_action(pets.COMMAND_ATTACK, pets.ACT_COMMAND),
            enemy_guid))
        assert state["forced_target"] == enemy_guid

        await socket.handle_pet_action(struct.pack(
            "<QIQ", pet_guid,
            pets.pack_action(pets.COMMAND_FOLLOW, pets.ACT_COMMAND), 0))
        assert state["command_state"] == pets.COMMAND_FOLLOW
        assert not state["forced_target"]

        await socket.handle_pet_action(struct.pack(
            "<QIQ", pet_guid,
            pets.pack_action(pets.COMMAND_STAY, pets.ACT_COMMAND), 0))
        assert state["command_state"] == pets.COMMAND_STAY
        assert state["stay_position"] == (
            float(spawn["x"]), float(spawn["y"]), float(spawn["z"]))

        for reaction in (pets.REACT_PASSIVE, pets.REACT_AGGRESSIVE,
                         pets.REACT_DEFENSIVE):
            sent.clear()
            await socket.handle_pet_action(struct.pack(
                "<QIQ", pet_guid,
                pets.pack_action(reaction, pets.ACT_REACTION), 0))
            assert state["react_state"] == reaction
            if reaction == pets.REACT_PASSIVE:
                assert state["forced_target"] == 0

    asyncio.run(flow())


def test_pet_autocast_and_action_bar_edits(tmp_path):
    async def flow():
        socket, sent, _peer_sent = _socket(tmp_path)
        low = await socket._summon_creature(416, -1, is_pet=True)
        pet_guid = creature.creature_guid(low, 416)
        state = socket._summon_state(low)

        sent.clear()
        await socket.handle_pet_spell_autocast(
            struct.pack("<QIB", pet_guid, 3110, 1))
        assert state["pet_autocast"] == {3110}
        parsed = _parse_pet_spells(sent[-1][1])
        assert parsed["bar"][3] == pets.pack_action(3110, pets.ACT_ENABLED)
        assert parsed["spells"][0] == pets.pack_action(
            3110, pets.ACT_ENABLED)

        await socket.handle_pet_spell_autocast(
            struct.pack("<QIB", pet_guid, 3110, 0))
        assert not state["pet_autocast"]
        parsed = _parse_pet_spells(sent[-1][1])
        assert parsed["bar"][3] == pets.pack_action(3110, pets.ACT_DISABLED)

        # Move the spell from slot 3 to slot 4 and clear slot 3 in one packet.
        await socket.handle_pet_set_action(struct.pack(
            "<QIIII", pet_guid, 3, 0, 4,
            pets.pack_action(3110, pets.ACT_DISABLED)))
        assert state["pet_action_bar"][3] == 0
        assert state["pet_action_bar"][4] == pets.pack_action(
            3110, pets.ACT_DISABLED)

        sent.clear()
        await socket.handle_request_pet_info(b"")
        assert sent and sent[-1][0] == int(Opcodes.SMSG_PET_SPELLS)

    asyncio.run(flow())


def test_pet_rename_and_name_query(tmp_path):
    async def flow():
        socket, sent, _peer_sent = _socket(tmp_path)
        low = await socket._summon_creature(416, -1, is_pet=True)
        spawn = socket._summoned_creatures[low]
        state = socket._summon_state(low)
        pet_guid = creature.creature_guid(low, 416)
        pet_number = state["pet_number"]

        sent.clear()
        await socket.handle_pet_rename(
            struct.pack("<Q", pet_guid) + b"Fluffy\x00\x00")
        assert spawn["name"] == "Fluffy"
        assert not spawn["pet_flags"] & pets.UNIT_PET_FLAG_CAN_BE_RENAMED
        response = next(payload for opcode, payload in sent
                        if opcode == int(Opcodes.SMSG_PET_NAME_QUERY_RESPONSE))
        number = struct.unpack_from("<I", response, 0)[0]
        assert number == pet_number
        assert b"Fluffy\x00" in response

        sent.clear()
        await socket.handle_pet_name_query(
            struct.pack("<IQ", pet_number, pet_guid))
        response = next(payload for opcode, payload in sent
                        if opcode == int(Opcodes.SMSG_PET_NAME_QUERY_RESPONSE))
        assert b"Fluffy\x00" in response

        sent.clear()
        await socket.handle_pet_rename(
            struct.pack("<Q", pet_guid) + b"X\x00\x00")
        assert any(opcode == int(Opcodes.SMSG_PET_NAME_INVALID)
                   for opcode, _payload in sent)

    asyncio.run(flow())


def test_pet_dismiss_removes_the_action_bar(tmp_path):
    async def flow():
        socket, sent, _peer_sent = _socket(tmp_path)
        low = await socket._summon_creature(416, -1, is_pet=True)
        pet_guid = creature.creature_guid(low, 416)
        sent.clear()
        await socket.handle_pet_abandon(struct.pack("<Q", pet_guid))
        assert low not in socket._summoned_creatures
        assert any(opcode == int(Opcodes.SMSG_DESTROY_OBJECT)
                   for opcode, _payload in sent)

    asyncio.run(flow())
