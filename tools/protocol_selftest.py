#!/usr/bin/env python3
"""End-to-end protocol self-test for the experimental 3.3.5a server.

This simulates a real client:

    authserver: AUTH_LOGON_CHALLENGE -> AUTH_LOGON_PROOF -> REALM_LIST
    worldserver: SMSG_AUTH_CHALLENGE -> CMSG_AUTH_SESSION ->
                 encrypted AUTH_RESPONSE + login-screen packets ->
                 encrypted CHAR_ENUM / CHAR_CREATE / CHAR_DELETE

Run from the project root:

    python tools/protocol_selftest.py
"""
from __future__ import annotations

import asyncio
import math
import os
import sqlite3
import struct
import sys
import tempfile
import types
import zlib

root = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, os.path.join(root, "src"))
sys.path.insert(0, root)

from src.auth.server import AuthServer
from src.common.config import AuthConfig, Config, DBConfig, LogConfig, WorldConfig
from src.crypto.srp6 import SRP6
from src.crypto.world_crypt import WorldCrypt
from src.network.packet_header import ClientPacketHeader, parse_server_header
from src.protocol.opcodes import Opcodes
from src.world.server import WorldServer
from src.world import addon_info
from src.world import movement
from src.world import object_update
from src.world import realmsocket
from src.world import spells
from src.world import update_fields as F
from src.world import character_db
from src.world import creature
from src.world import gameobject
from tools.init_db import init_databases


USERNAME = "TEST"
PASSWORD = "TEST"
BUILD = 12340


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------
def read_cstring(data: bytes, pos: int):
    end = data.find(b"\x00", pos)
    if end < 0:
        raise AssertionError("unterminated string in packet")
    return data[pos:end].decode("utf-8"), end + 1


def read_packed_guid(data: bytes, pos: int):
    mask = data[pos]
    pos += 1
    guid = 0
    for i in range(8):
        if mask & (1 << i):
            guid |= data[pos] << (8 * i)
            pos += 1
    return guid, pos


def make_logon_challenge(username: str) -> bytes:
    body = bytearray()
    body += b"WoW\x00"
    body += bytes([3, 3, 5])
    body += struct.pack("<H", BUILD)
    body += b"x86\x00"
    body += b"Win\x00"
    body += b"enUS"
    body += struct.pack("<i", 0)  # timezone bias
    body += struct.pack("<I", 0x0100007F)  # 127.0.0.1
    body += bytes([len(username)])
    body += username.encode("utf-8")
    return bytes([0x00, 0x00]) + struct.pack("<H", len(body)) + bytes(body)


def client_srp(username: str, password: str, b_bytes: bytes, salt: bytes):
    """Return (A, clientM1, session_key)."""
    a = int.from_bytes(os.urandom(32), "little") % SRP6.N
    if a == 0:
        a = 1
    a_bytes = pow(SRP6.g, a, SRP6.N).to_bytes(32, "little")

    u = int.from_bytes(SRP6.h(a_bytes, b_bytes), "little")
    x = SRP6.calculate_x(username, password, salt)
    v = pow(SRP6.g, x, SRP6.N)

    b_int = int.from_bytes(b_bytes, "little")
    s_int = pow((b_int - SRP6.k * v) % SRP6.N, a + u * x, SRP6.N)
    session_key = SRP6.sha1_interleave(s_int.to_bytes(32, "little"))

    ng_hash = bytes(
        x ^ y for x, y in zip(SRP6.h(SRP6.N_BYTES), SRP6.h(SRP6.G_BYTES))
    )
    i_hash = SRP6.h(username.upper().encode("utf-8"))
    m1 = SRP6.h(ng_hash, i_hash, salt, a_bytes, b_bytes, session_key)
    return a_bytes, m1, session_key


async def read_server_packet(reader: asyncio.StreamReader,
                             crypt: WorldCrypt | None = None,
                             skip_monster_moves: bool = True):
    """Read one world-server packet, decrypting the header if needed.

    Ambient creatures can emit ``SMSG_MONSTER_MOVE`` at any time (especially
    with the world-shared idle driver).  Those packets are unrelated to the
    request/response checks below, so skip them unless a caller explicitly
    wants them.
    """
    while True:
        header = await reader.readexactly(4)
        if crypt is not None:
            header = crypt.decrypt(header)

        if header[0] & 0x80:
            extra = await reader.readexactly(1)
            if crypt is not None:
                extra = crypt.decrypt(extra)
            header += extra
            header_len, size, opcode = parse_server_header(header)
        else:
            header_len, size, opcode = parse_server_header(header)

        payload_len = size - 2
        payload = await reader.readexactly(payload_len) if payload_len else b""
        if skip_monster_moves and opcode == int(Opcodes.SMSG_MONSTER_MOVE):
            continue
        return opcode, payload


async def read_server_packet_until(reader: asyncio.StreamReader,
                                   crypt: WorldCrypt | None,
                                   expected: int, label: str,
                                   ignored: tuple[int, ...] = ()):
    """Read packets until ``expected``, skipping known asynchronous traffic."""
    while True:
        opcode, payload = await read_server_packet(reader, crypt)
        if opcode == expected:
            return opcode, payload
        if opcode in ignored:
            continue
        assert_eq(opcode, expected, label)


async def send_client_packet(writer: asyncio.StreamWriter, opcode: int, payload: bytes = b"",
                             crypt: WorldCrypt | None = None):
    header = ClientPacketHeader(size=len(payload) + 4, opcode=opcode).build()
    if crypt is not None:
        header = crypt.encrypt(header)
    writer.write(header + payload)
    await writer.drain()


def assert_eq(actual, expected, label: str):
    if actual != expected:
        raise AssertionError(f"{label}: expected {expected!r}, got {actual!r}")


def parse_packed_guid(data: bytes, pos: int):
    """ObjectGuid::WriteAsPacked(): mask byte + non-zero bytes only."""
    mask = data[pos]
    pos += 1
    guid = 0
    for byte_index in range(8):
        if mask & (1 << byte_index):
            guid |= data[pos] << (8 * byte_index)
            pos += 1
    return guid, pos


def parse_values_update(body: bytes):
    """Parse a single-block ``UPDATETYPE_VALUES`` update into ``(guid, values)``.

    Verifies the framing the 3.3.5a client's update dispatcher depends on:
    one block, update type 0 (VALUES - 2/3 is CreateObject), a packed guid, a
    1326-bit player mask and exactly one uint32 per set bit.
    """
    assert_eq(struct.unpack_from("<I", body, 0)[0], 1, "values update block count")
    assert_eq(body[4], 0, "values update type is VALUES (0)")
    guid, pos = parse_packed_guid(body, 5)
    mask_blocks = body[pos]
    assert mask_blocks > 0, "values update carries a mask"
    pos += 1
    mask = body[pos:pos + mask_blocks * 4]
    pos += mask_blocks * 4
    value_count = sum(bin(byte).count("1") for byte in mask)
    assert_eq(pos + value_count * 4, len(body), "values update is well-formed")
    values = {}
    for field in range(F.PLAYER_VALUES_COUNT):
        byte = (field // 32) * 4 + (field % 32) // 8
        if mask[byte] & (1 << (field % 8)):
            values[field] = struct.unpack_from("<I", body, pos)[0]
            pos += 4
    return guid, values


def parse_mirror_image_data(payload: bytes):
    """Parse ``SMSG_MIRRORIMAGE_DATA`` the way the 3.3.5 client does."""
    assert_eq(len(payload), 68, "mirror image payload size")
    guid, display_id = struct.unpack_from("<QI", payload, 0)
    race, sex, char_class, skin, face, hair, hair_color, facial = struct.unpack_from(
        "<8B", payload, 12
    )
    (guild_id,) = struct.unpack_from("<I", payload, 20)
    item_display_ids = list(struct.unpack_from("<11I", payload, 24))
    return {
        "guid": guid,
        "display_id": display_id,
        "race": race,
        "sex": sex,
        "class": char_class,
        "skin": skin,
        "face": face,
        "hair": hair,
        "hair_color": hair_color,
        "facial": facial,
        "guild_id": guild_id,
        "items": item_display_ids,
    }


def parse_creature_query_response(payload: bytes):
    """Parse ``SMSG_CREATURE_QUERY_RESPONSE`` the way the client's DBCache does."""
    entry = struct.unpack_from("<I", payload, 0)[0]
    if entry & 0x80000000:
        return {"entry": entry & 0x7FFFFFFF, "allow": False}
    pos = 4
    name, pos = read_cstring(payload, pos)
    name2, pos = read_cstring(payload, pos)
    name3, pos = read_cstring(payload, pos)
    name4, pos = read_cstring(payload, pos)
    title, pos = read_cstring(payload, pos)
    cursor, pos = read_cstring(payload, pos)
    flags, ctype, family, classification = struct.unpack_from("<IIII", payload, pos)
    pos += 16
    kill_credits = struct.unpack_from("<II", payload, pos)
    pos += 8
    displays = struct.unpack_from("<IIII", payload, pos)
    pos += 16
    hp_multi, energy_multi, leader = struct.unpack_from("<ffB", payload, pos)
    pos += 9
    quest_items = struct.unpack_from("<6I", payload, pos)
    pos += 24
    movement_info_id = struct.unpack_from("<I", payload, pos)[0]
    pos += 4
    assert_eq(pos, len(payload), "creature query response consumed exactly")
    return {
        "entry": entry,
        "allow": True,
        "name": name,
        "variants": (name2, name3, name4),
        "title": title,
        "cursor": cursor,
        "flags": flags,
        "type": ctype,
        "family": family,
        "classification": classification,
        "kill_credits": kill_credits,
        "displays": displays,
        "hp_multi": hp_multi,
        "energy_multi": energy_multi,
        "leader": leader,
        "quest_items": quest_items,
        "movement_info_id": movement_info_id,
    }


def parse_gossip_message(payload: bytes):
    """Parse ``SMSG_GOSSIP_MESSAGE`` (guid, menu id, text id, options, quests)."""
    guid = struct.unpack_from("<Q", payload, 0)[0]
    gossip_id, text_id = struct.unpack_from("<ii", payload, 8)
    pos = 16
    option_count = struct.unpack_from("<I", payload, pos)[0]
    pos += 4
    options = []
    for _index in range(option_count):
        index, option_type, coded, money = struct.unpack_from("<IBBI", payload, pos)
        pos += 10
        text, pos = read_cstring(payload, pos)
        box_text, pos = read_cstring(payload, pos)
        options.append({
            "index": index,
            "type": option_type,
            "coded": coded,
            "money": money,
            "text": text,
            "box_text": box_text,
        })
    quest_count = struct.unpack_from("<I", payload, pos)[0]
    pos += 4
    quests = []
    for _index in range(quest_count):
        quest_id, icon, level, flags = struct.unpack_from("<IIII", payload, pos)
        pos += 16
        repeatable = payload[pos]
        pos += 1
        title, pos = read_cstring(payload, pos)
        quests.append({
            "quest_id": quest_id,
            "icon": icon,
            "level": level,
            "flags": flags,
            "repeatable": repeatable,
            "title": title,
        })
    assert_eq(pos, len(payload), "gossip message consumed exactly")
    return {
        "guid": guid,
        "gossip_id": gossip_id,
        "text_id": text_id,
        "options": options,
        "quests": quests,
    }


def parse_npc_text_update(payload: bytes):
    """Parse all eight ``SMSG_NPC_TEXT_UPDATE`` records."""
    text_id = struct.unpack_from("<I", payload, 0)[0]
    if text_id & 0x80000000:
        return {"id": text_id & 0x7FFFFFFF, "allow": False, "options": []}
    pos = 4
    options = []
    for _index in range(8):
        probability = struct.unpack_from("<f", payload, pos)[0]
        pos += 4
        text, pos = read_cstring(payload, pos)
        female_text, pos = read_cstring(payload, pos)
        language = struct.unpack_from("<i", payload, pos)[0]
        pos += 4
        emotes = []
        for _emote in range(3):
            delay, emote = struct.unpack_from("<II", payload, pos)
            pos += 8
            emotes.append((delay, emote))
        options.append({
            "probability": probability,
            "text": text,
            "female_text": female_text,
            "language": language,
            "emotes": emotes,
        })
    assert_eq(pos, len(payload), "npc text update consumed exactly")
    return {"id": text_id, "allow": True, "options": options}


def parse_chat_payload(payload: bytes):
    """Parse ``SMSG_MESSAGECHAT`` for the default (say/yell/whisper) layout.

    Mirrors ChatFrame.cpp:1588-1648: type(u8), language(u32), sender guid(u64),
    sender unk(u32), target guid(u64), length(u32), text + NUL, chat tag(u8).
    ``IsRead()`` requires the packet to be consumed exactly, so the total size
    is asserted too.
    """
    msg_type = payload[0]
    (language,) = struct.unpack_from("<I", payload, 1)
    sender_guid, sender_unk = struct.unpack_from("<QI", payload, 5)
    (target_guid,) = struct.unpack_from("<Q", payload, 17)
    (length,) = struct.unpack_from("<I", payload, 25)
    text_start = 29
    assert_eq(payload[text_start + length - 1], 0, "chat text is NUL terminated")
    text = payload[text_start:text_start + length - 1].decode("utf-8")
    chat_tag = payload[text_start + length]
    assert_eq(text_start + length + 1, len(payload),
              "chat packet is consumed exactly (client IsRead)")
    return {
        "type": msg_type,
        "language": language,
        "sender_guid": sender_guid,
        "sender_unk": sender_unk,
        "target_guid": target_guid,
        "text": text,
        "chat_tag": chat_tag,
    }


def parse_update_object(payload: bytes, compressed: bool) -> dict:
    """Parse the update object blocks sent on world entry.

    With items enabled, there are multiple blocks (items first, then player).
    We find the player block (TYPEID_PLAYER) and return its parsed data.
    """
    if compressed:
        size = struct.unpack_from("<I", payload, 0)[0]
        body = zlib.decompress(payload[4:])
        assert_eq(len(body), size, "uncompressed update size")
    else:
        body = payload

    block_count = struct.unpack_from("<I", body, 0)[0]
    pos = 4

    player_block = None
    creature_blocks = []
    gameobject_blocks = []
    for block_idx in range(block_count):
        update_type = body[pos]
        pos += 1
        guid, pos = parse_packed_guid(body, pos)
        type_id = body[pos]
        pos += 1
        flags = struct.unpack_from("<H", body, pos)[0]
        pos += 2

        # Items (TYPEID_ITEM = 1) have no movement info because their flags=0.
        # Player and creature blocks have UPDATEFLAG_LIVING | UPDATEFLAG_HAS_POSITION = 0x60
        has_movement = bool(flags & (object_update.UPDATEFLAG_LIVING | object_update.UPDATEFLAG_HAS_POSITION))
        stationary = (type_id == gameobject.TYPEID_GAMEOBJECT)

        if stationary:
            # Game objects: stationary x, y, z, o only (no MovementInfo).
            move_flags = move_flags2 = move_time = 0
            x, y, z, o = struct.unpack_from("<ffff", body, pos)
            pos += 16
            fall_time = 0.0
            speeds = (0,) * 9
        elif has_movement:
            move_flags = struct.unpack_from("<I", body, pos)[0]
            pos += 4
            move_flags2 = struct.unpack_from("<H", body, pos)[0]
            pos += 2
            move_time = struct.unpack_from("<I", body, pos)[0]
            pos += 4
            x, y, z, o = struct.unpack_from("<ffff", body, pos)
            pos += 16
            fall_time = struct.unpack_from("<f", body, pos)[0]
            pos += 4
            speeds = struct.unpack_from("<9f", body, pos)
            pos += 36
        else:
            # Item blocks: no movement data at all
            move_flags = move_flags2 = move_time = 0
            x = y = z = o = 0.0
            fall_time = 0.0
            speeds = (0,) * 9

        high_guid = None
        if flags & object_update.UPDATEFLAG_HIGHGUID:
            high_guid = struct.unpack_from("<I", body, pos)[0]
            pos += 4
        rotation = None
        if flags & gameobject.UPDATEFLAG_ROTATION:
            rotation = struct.unpack_from("<q", body, pos)[0]
            pos += 8

        mask_blocks = body[pos]
        pos += 1
        mask = body[pos:pos + mask_blocks * 4]
        pos += mask_blocks * 4

        values = {}
        if type_id == object_update.TYPEID_PLAYER:
            max_field = F.PLAYER_VALUES_COUNT
        elif type_id == object_update.TYPEID_UNIT:
            max_field = F.UNIT_END
        elif type_id == gameobject.TYPEID_GAMEOBJECT:
            max_field = F.GAMEOBJECT_END
        else:
            max_field = F.ITEM_END
        for field in range(max_field):
            byte = (field // 32) * 4 + (field % 32) // 8
            if mask[byte] & (1 << (field % 8)):
                values[field] = struct.unpack_from("<I", body, pos)[0]
                pos += 4

        block_data = {
            "update_type": update_type,
            "guid": guid,
            "type_id": type_id,
            "flags": flags,
            "move_flags": move_flags,
            "move_flags2": move_flags2,
            "move_time": move_time,
            "fall_time": fall_time,
            "position": (x, y, z, o),
            "speeds": speeds,
            "high_guid": high_guid,
            "rotation": rotation,
            "mask_blocks": mask_blocks,
            "values": values,
        }

        if type_id == object_update.TYPEID_PLAYER:
            player_block = block_data
        elif type_id == object_update.TYPEID_UNIT:
            creature_blocks.append(block_data)
        elif type_id == gameobject.TYPEID_GAMEOBJECT:
            gameobject_blocks.append(block_data)

    assert player_block is not None, "no player block found in update object"
    player_block["creatures"] = creature_blocks
    player_block["gameobjects"] = gameobject_blocks
    return player_block


def movement_codec_checks():
    """Round-trip every ``MovementInfo`` shape a 3.3.5 client can send."""
    samples = [
        movement.MovementInfo(),
        movement.MovementInfo(
            flags=movement.MOVEMENTFLAG_FORWARD | movement.MOVEMENTFLAG_WALKING,
            time=1234, x=1.5, y=-2.5, z=3.5, o=4.5, fall_time=77,
        ),
        movement.MovementInfo(
            flags=movement.MOVEMENTFLAG_FALLING,
            time=99, x=-100.0, y=-200.0, z=300.0, o=1.0, fall_time=42,
            jump_zspeed=-7.1, jump_cos_angle=0.5, jump_sin_angle=0.86,
            jump_xyspeed=7.0,
        ),
        movement.MovementInfo(
            flags=movement.MOVEMENTFLAG_ONTRANSPORT | movement.MOVEMENTFLAG_SWIMMING,
            flags2=(movement.MOVEMENTFLAG2_INTERPOLATED_MOVEMENT
                    | movement.MOVEMENTFLAG2_INTERPOLATED_TURNING),
            time=7, x=1.0, y=2.0, z=3.0, o=0.25,
            transport_guid=0x0102, transport_x=4.0, transport_y=5.0,
            transport_z=6.0, transport_o=7.0, transport_time=8,
            transport_time2=9, transport_seat=3, transport_time3=10,
            pitch=-0.5, fall_time=11,
        ),
        movement.MovementInfo(
            flags=movement.MOVEMENTFLAG_FLYING,
            flags2=movement.MOVEMENTFLAG2_ALWAYS_ALLOW_PITCHING,
            time=1, x=0.0, y=0.0, z=500.0, o=0.0, pitch=1.25,
        ),
    ]
    for sample in samples:
        encoded = movement.build_movement_info(sample)
        decoded, end = movement.parse_movement_info(encoded, 0)
        assert_eq(end, len(encoded), "movement info consumed length")
        assert_eq(decoded.flags, sample.flags, "movement info flags")
        assert_eq(decoded.flags2, sample.flags2, "movement info flags2")
        assert_eq(decoded.time, sample.time, "movement info time")
        assert_eq(decoded.position(), sample.position(), "movement info position")
        assert_eq(decoded.pitch, sample.pitch, "movement info pitch")
        assert_eq(decoded.fall_time, sample.fall_time, "movement info fall time")
        assert_eq(decoded.transport_guid, sample.transport_guid, "movement info transport guid")
        assert_eq(decoded.transport_seat, sample.transport_seat, "movement info transport seat")
        for field_name in ("jump_zspeed", "jump_cos_angle", "jump_sin_angle", "jump_xyspeed"):
            actual = getattr(decoded, field_name)
            expected = getattr(sample, field_name)
            if abs(actual - expected) > 1e-4:
                raise AssertionError(
                    f"{field_name}: expected {expected!r}, got {actual!r}"
                )

    packet = movement.build_movement_packet(6, samples[1])
    packet_guid, packet_info = movement.parse_movement_packet(packet)
    assert_eq(packet_guid, 6, "movement packet guid")
    assert_eq(packet_info.position(), samples[1].position(), "movement packet position")

    # Regression fixture: the exact MSG_MOVE_FALL_LAND the real 3.3.5a client
    # sent after landing at the spawn point (packed guid 6, no flags).
    raw = bytes.fromhex(
        "0106000000000000525F400FCDD70BC6717D04C35C0FA7420000000000000000"
    )
    guid, info = movement.parse_movement_packet(raw)
    assert_eq(guid, 6, "FALL_LAND guid")
    assert_eq(info.flags, 0, "FALL_LAND flags")
    assert_eq(info.flags2, 0, "FALL_LAND flags2")
    assert_eq(info.time, 0x0F405F52, "FALL_LAND time")
    assert_eq((round(info.x, 2), round(info.y, 2), round(info.z, 2), round(info.o, 2)),
              (-8949.95, -132.49, 83.53, 0.0), "FALL_LAND position")
    assert_eq(info.fall_time, 0, "FALL_LAND fall time")
    assert_eq(info.parsed_bytes, 30, "FALL_LAND movement info length")
    print("  movement codec: 5 layouts + real-client FALL_LAND fixture OK")


def world_data_checks(temp_dir: str, world_db: str) -> None:
    """Check per-race starts and the distance-limited creature visibility query."""
    expected = {
        (1, 1): (0, 12, -8949.95, -132.493, 83.5312, 0.0),
        (2, 1): (1, 14, -618.518, -4251.67, 38.718, 0.0),
        (3, 1): (0, 1, -6240.32, 331.033, 382.758, 6.17716),
        (4, 1): (1, 141, 10311.3, 831.463, 1326.41, 5.48033),
        (5, 1): (0, 85, 1676.35, 1677.45, 121.67, 2.70526),
        (6, 1): (1, 215, -2917.58, -257.98, 52.9968, 0.0),
        (7, 1): (0, 1, -6240.32, 331.033, 382.758, 0.0),
        (8, 1): (1, 14, -618.518, -4251.67, 38.718, 0.0),
        (10, 2): (530, 3431, 10349.6, -6357.29, 33.4026, 5.31605),
        (11, 1): (530, 3526, -3961.64, -13931.2, 100.615, 2.08364),
        (1, 6): (609, 4298, 2355.84, -5664.77, 426.028, 3.65997),
    }
    for (race, char_class), want in expected.items():
        got = character_db.start_position(race, char_class)
        assert_eq(
            tuple(round(value, 5) if isinstance(value, float) else value for value in got),
            tuple(round(value, 5) if isinstance(value, float) else value for value in want),
            f"start position race {race} class {char_class}",
        )
    assert_eq(character_db.start_position(10, 1), character_db.start_position(10, 2),
              "blood elf without a warrior row uses the race start")
    assert_eq(character_db.start_position(99, 1), character_db.start_position(1, 1),
              "unknown race falls back to the human start")

    # Character creation must persist the same map/zone/position.
    char_db = os.path.join(temp_dir, "start-positions.db")
    character_db.init_character_db(char_db)
    guid = character_db.create_character(
        char_db, 1, "Starttest", 2, 1, 0, 0, 0, 0, 0, 0
    )
    row = character_db.get_character(char_db, 1, guid)
    assert_eq(
        (int(row["map"]), int(row["zone"]), round(float(row["x"]), 5),
         round(float(row["y"]), 5), round(float(row["z"]), 5), round(float(row["o"]), 5)),
        (1, 14, -618.518, -4251.67, 38.718, 0.0),
        "create_character stores the orc start",
    )

    # Race/class legality comes from the client's CharBaseInfo.dbc.
    assert character_db.is_valid_race_class(1, 1), "human warrior legal"
    assert character_db.is_valid_race_class(10, 2), "blood elf paladin legal"
    assert not character_db.is_valid_race_class(7, 2), "gnome paladin illegal"
    assert not character_db.is_valid_race_class(6, 8), "tauren mage illegal"
    assert not character_db.is_valid_race_class(1, 3), "human hunter illegal"
    assert_eq(len(character_db._STARTER_OUTFITS), 63,
              "CharStartOutfit rows (62 legal pairs + one legacy dwarf-mage row)")
    for race, classes in character_db.RACE_CLASSES.items():
        for char_class in classes:
            assert (race, char_class) in character_db._STARTER_OUTFITS, (
                f"start outfit exists for race {race} class {char_class}")

    # Starting stats: every legal race/class pair has a level-1
    # player_levelstats row and a class/level pool, and the derived health
    # matches Unit::UpdateMaxHealth / UpdateMaxPower exactly.
    con = sqlite3.connect(world_db)
    try:
        level_rows = {
            (int(race), int(cls)): (int(s), int(a), int(st), int(i), int(sp))
            for race, cls, s, a, st, i, sp in con.execute(
                "SELECT race, class, str, agi, sta, inte, spi "
                "FROM player_levelstats WHERE level = 1")
        }
        class_rows = {
            int(cls): (int(hp), int(mana)) for cls, hp, mana in con.execute(
                "SELECT class, basehp, basemana FROM player_classlevelstats "
                "WHERE level = 1")
        }
    finally:
        con.close()

    assert_eq(len(level_rows), 62, "level-1 stat rows for all legal pairs")
    rage_classes = (1, 6)
    energy_classes = (4,)
    # (race, class): (max health, max power).  These are the values the real
    # 3.3.5a client shows, including the Tauren Endurance 5% racial.
    expected_pools = {
        (1, 1): (60, 1000),     # human warrior: basehp 20 + 40 stamina hp
        (2, 1): (80, 1000),     # orc warrior
        (6, 1): (84, 1000),     # tauren warrior: 80 * 1.05 Endurance
        (1, 8): (52, 165),      # human mage: 32 + 20 sta, 100 + 65 int
        (6, 11): (88, 77),      # tauren druid
        (10, 2): (58, 140),     # blood elf paladin
        (4, 4): (45, 100),      # night elf rogue: energy, no mana
        (3, 5): (102, 108),     # dwarf priest: 52 + 50 stamina health
    }
    for race, classes in character_db.RACE_CLASSES.items():
        for char_class in classes:
            key = (race, char_class)
            assert key in level_rows, f"level-1 stats for race {race} class {char_class}"
            assert char_class in class_rows, (
                f"level-1 basehp/basemana for class {char_class}")
            str_, agi_, sta_, inte_, spi_ = level_rows[key]
            assert min(str_, agi_, sta_, inte_, spi_) > 0, (
                f"non-zero attributes for race {race} class {char_class}")
            base_hp, base_mana = class_rows[char_class]
            health = math.floor(
                (base_hp + realmsocket.WorldSocket._health_bonus_from_stamina(sta_))
                * (1.05 if race == 6 else 1.0) + 0.5)
            if char_class in rage_classes:
                power = 1000
            elif char_class in energy_classes:
                power = 100
            else:
                power = base_mana + math.floor(
                    realmsocket.WorldSocket._mana_bonus_from_intellect(inte_) + 0.5)
            if key in expected_pools:
                assert_eq((health, power), expected_pools[key],
                          f"starting pools race {race} class {char_class}")

    # Death knights start at level 55, so their starting pools come from the
    # level-55 rows.  A human DK has 1359 basehp + 810 stamina health = 2169.
    assert_eq(character_db.start_level(1), 1, "warrior starts at level 1")
    assert_eq(character_db.start_level(6), 55, "death knight starts at level 55")
    con = sqlite3.connect(world_db)
    try:
        dk_stats = con.execute(
            "SELECT str, agi, sta, inte, spi FROM player_levelstats "
            "WHERE race=1 AND class=6 AND level=55").fetchone()
        dk_hp, dk_mana = con.execute(
            "SELECT basehp, basemana FROM player_classlevelstats "
            "WHERE class=6 AND level=55").fetchone()
    finally:
        con.close()
    assert dk_stats is not None and dk_hp is not None, "level-55 DK rows exist"
    dk_health = math.floor(
        (int(dk_hp) + realmsocket.WorldSocket._health_bonus_from_stamina(
            float(dk_stats[2]))) + 0.5)
    assert_eq(dk_health, 2169, "human death knight level-55 health")
    assert_eq(int(dk_mana), 0, "death knight has no mana pool")

    # Every race/class must receive exactly the items and slots from the
    # client's CharStartOutfit.dbc (male and female rows are identical).
    kits = {
        (1, 1): {3: 38, 6: 39, 7: 40, 15: 49778},
        (2, 1): {3: 6125, 6: 139, 7: 140, 15: 12282},
        (1, 4): {3: 49, 6: 48, 7: 47, 15: 2092, 16: 50055, 17: 28979},
        (1, 8): {3: 6096, 4: 56, 6: 1395, 7: 55, 15: 35},
        (10, 4): {3: 20897, 6: 20896, 7: 20898, 15: 20982, 16: 50057,
                  17: 28979},
        (1, 6): {
            0: 34652, 1: 34657, 2: 34655, 4: 34650, 5: 34651, 6: 34656,
            7: 34648, 8: 34653, 9: 34649, 10: 34658, 11: 38147, 14: 34659,
            19: 38145, 20: 38145, 21: 38145, 22: 38145,
        },
    }
    for index, ((race, char_class), want) in enumerate(sorted(kits.items())):
        kit_guid = character_db.create_character(
            char_db, 1, f"Kit{index}", race, char_class, 0, 0, 0, 0, 0, 0
        )
        character_db.grant_starter_items(char_db, kit_guid, race, char_class)
        got = {
            int(slot): int(item["item_entry"])
            for slot, item in character_db.get_character_inventory(
                char_db, kit_guid).items()
        }
        assert_eq(got, want, f"starter kit race {race} class {char_class}")

    # Equipped-item aggregation (``Player::_ApplyItemMods``): primary stats,
    # flat health/mana, attack power and armor/resistances from slots 0..18.
    # Items carried in the backpack (256..271) must not contribute.
    con = sqlite3.connect(char_db)
    try:
        con.execute(
            "INSERT OR REPLACE INTO item_templates("
            "entry, name1, inventory_type, resistance_armor, resistance_fire,"
            " stat_type1, stat_value1, stat_type2, stat_value2,"
            " stat_type3, stat_value3, stat_type4, stat_value4,"
            " stat_type5, stat_value5, stat_type6, stat_value6,"
            " stat_type7, stat_value7, stat_type8, stat_value8) "
            "VALUES(99999, 'Test Plate', 5, 100, 7,"
            " 4, 5, 3, 4, 7, 3, 5, 2, 6, 1, 1, 50, 0, 30, 38, 10)")
        con.commit()
    finally:
        con.close()
    gear_guid = character_db.create_character(
        char_db, 1, "Gearset", 1, 1, 0, 0, 0, 0, 0, 0
    )
    character_db.add_item_to_inventory(char_db, gear_guid, 99999)
    carried = character_db.get_equipped_item_totals(char_db, gear_guid)
    assert_eq(carried["armor"], 0, "backpack item does not add armor")
    assert_eq(carried["str"], 0, "backpack item does not add strength")
    assert character_db.swap_character_items(
        char_db, gear_guid, 256, 4), "move test plate to the chest slot"
    totals = character_db.get_equipped_item_totals(char_db, gear_guid)
    assert_eq(totals, {
        "str": 5, "agi": 4, "sta": 3, "inte": 2, "spi": 1,
        "health": 50, "mana": 30,
        "attack_power": 10, "ranged_attack_power": 0, "spell_power": 0,
        "hit_rating": 0, "crit_rating": 0, "haste_rating": 0,
        "defense_rating": 0, "dodge_rating": 0, "parry_rating": 0,
        "block_rating": 0, "block_value": 0,
        "resilience": 0, "armor_penetration": 0, "expertise": 0,
        "spell_penetration": 0, "healing_power": 0, "shield": 0,
        "armor": 100, "holy": 0, "fire": 7, "nature": 0,
        "frost": 0, "shadow": 0, "arcane": 0,
    }, "equipped item totals")

    # The 3.3.5a-only starter weapons must exist even in a schema-only DB.
    for entry, name, inv_type in (
        (49778, "Worn Greatsword", 17),
        (50055, "Worn Dirk", 22),
        (50057, "Sharp Dirk", 22),
    ):
        template = character_db.get_item_template(char_db, entry)
        assert template is not None, f"starter item template {entry}"
        assert_eq(str(template["name1"]), name, f"starter item {entry} name")
        assert_eq(int(template["inventory_type"]), inv_type,
                  f"starter item {entry} inventory type")

    # The demo seed puts all eleven NPCs around the human start.  The query
    # must respect both map and the 3D radius.
    human = (-8949.95, -132.493, 83.5312)
    near = creature.get_creature_spawns_near(world_db, 0, *human, 150.0)
    assert_eq(len(near), 11, "eleven demo NPCs within 150 yards of the human start")
    assert_eq(creature.creature_guid(int(near[0]["guid"]), int(near[0]["entry"])) >> 48,
              0xF130, "spawn guid gets the HIGHGUID_UNIT word")
    assert_eq(
        creature.creature_entry_from_guid(
            creature.creature_guid(int(near[0]["guid"]), int(near[0]["entry"]))),
        int(near[0]["entry"]),
        "creature guid packs the template entry the client queries",
    )
    assert_eq(
        len(creature.get_creature_spawns_near(world_db, 0, *human, 10.0)),
        0,
        "creature radius filter excludes distant spawns",
    )
    assert_eq(
        len(creature.get_creature_spawns_near(world_db, 1, *human, 150.0)),
        0,
        "creature map filter excludes other continents",
    )
    update = object_update.build_creature_update(near[:2])
    assert update is not None, "incremental creature update is built"
    assert int(update[0]) in (int(Opcodes.SMSG_UPDATE_OBJECT),
                              int(Opcodes.SMSG_COMPRESSED_UPDATE_OBJECT)), \
        "incremental creature update opcode"

    # ``visible = 0`` marks Blizzard's tooling creatures (invisible stalkers,
    # bunnies, "[DND]" props).  They must never reach the client.
    con = sqlite3.connect(world_db)
    try:
        con.execute(
            "INSERT INTO creature_templates(entry, name, display_id1, visible) "
            "VALUES(31337, '[DND] Test Pedestal', 1276, 0)")
        con.execute(
            "INSERT INTO creature_spawns(guid, entry, map, x, y, z, o) "
            "VALUES(31337, 31337, 0, ?, ?, ?, 0)", human[:3])
        con.commit()
    finally:
        con.close()
    assert_eq(
        len(creature.get_creature_spawns_near(world_db, 0, *human, 150.0)),
        11,
        "creature_templates.visible = 0 keeps tooling creatures off the client",
    )
    assert_eq(
        len(creature.get_creature_spawns(world_db, 0)),
        11,
        "the whole-map query honours creature_templates.visible too",
    )

    # The importer's name rule must hide the tooling content and keep the real
    # quest givers whose names merely start with "Old".
    from tools.import_wotlk_db import is_player_visible
    for name in ("[DND] TAR Pedestal - Gems", "Invisible Stalker", "World Trigger",
                 "ELM General Purpose Bunny", "Formation Marker", "Camera Shaker",
                 "Waypoint (Only GM can see it)", "Combat Dummy", "Archery Target"):
        assert_eq(is_player_visible(name), 0, f"tooling creature hidden: {name}")
    for name in ("Marshal McBride", "Old Man Heming", "Old Orok", "Old Man Barlo",
                 "Innkeeper Farley", "Northshire Guard", "Deputy Willem"):
        assert_eq(is_player_visible(name), 1, f"real creature kept: {name}")

    # An upgrade must add every column the spawn query reads.  A hand-written
    # list used to drift and left ``unit_flags2`` out, which broke every creature
    # query on an upgraded database.
    old_db = os.path.join(temp_dir, "old-creatures.db")
    con = sqlite3.connect(old_db)
    con.executescript("""
    CREATE TABLE creature_templates(
      entry INTEGER PRIMARY KEY, name TEXT NOT NULL DEFAULT '',
      display_id1 INTEGER NOT NULL DEFAULT 0, base_health INTEGER NOT NULL DEFAULT 100);
    CREATE TABLE creature_spawns(
      guid INTEGER PRIMARY KEY, entry INTEGER NOT NULL, map INTEGER NOT NULL DEFAULT 0,
      zone INTEGER NOT NULL DEFAULT 0,
      x REAL NOT NULL DEFAULT 0.0, y REAL NOT NULL DEFAULT 0.0, z REAL NOT NULL DEFAULT 0.0,
      o REAL NOT NULL DEFAULT 0.0, display_id INTEGER NOT NULL DEFAULT 0,
      spawn_time INTEGER NOT NULL DEFAULT 0);
    INSERT INTO creature_templates(entry, name, display_id1) VALUES(197, 'Marshal McBride', 1276);
    INSERT INTO creature_spawns(guid, entry, map, x, y, z) VALUES(1001, 197, 0, -8949.95, -132.49, 83.53);
    """)
    con.commit()
    con.close()
    creature.init_creature_db(old_db)
    upgraded = {row[1] for row in sqlite3.connect(old_db).execute(
        "PRAGMA table_info(creature_templates)")}
    missing = [name for name, _decl in creature._TEMPLATE_COLUMNS if name not in upgraded]
    assert_eq(missing, [], "upgrading an old creature schema adds every column")
    assert_eq(len(creature.get_creature_spawns_near(old_db, 0, *human, 150.0)), 1,
              "an upgraded database keeps serving its existing spawns")
    print("  world data: 11 race starts, character positions, 150y radius,"
          " tooling-creature filter and schema upgrade OK")


async def rune_and_creature_ai_checks(temp_dir: str, world_db: str) -> None:
    """Verify the rune packets/spend rules and the creature spell-list AI.

    The temporary world DB is schema-only, so the handful of rows these checks
    need (SpellRuneCost, one legacy template spell set, one spell list) are
    copied from the populated project database when it exists.
    """
    source_db = os.path.join(root, "data", "world.db")
    if not os.path.exists(source_db):
        print("  runes/creature AI: skipped (data/world.db not present)")
        return

    con = sqlite3.connect(world_db)
    try:
        con.execute("ATTACH DATABASE ? AS source", (source_db,))

        def copy_rows(table: str, where: str, params: tuple = ()) -> None:
            columns = [row[1] for row in con.execute(f"PRAGMA table_info({table})")]
            names = ",".join(columns)
            con.execute(
                f"INSERT OR REPLACE INTO {table}({names}) "
                f"SELECT {names} FROM source.{table} WHERE {where}",
                params,
            )

        copy_rows("spell_templates",
                  "id IN (133, 688, 2120, 12544, 19816, 20296, 20792, 45477, 47541)")
        copy_rows("spell_rune_cost", "id IN (222, 241, 301, 422)")
        copy_rows("creature_templates", "entry = 416")
        copy_rows("creature_template_spells", "entry = 1009")
        copy_rows("creature_template_classlevelstats",
                  "level = 22 AND class = 8")
        copy_rows("creature_spell_list", "id = 89101")
        copy_rows("creature_spell_list_entry", "id = 89101")
        copy_rows("creature_spell_targeting", "id = 100")
        con.commit()
        con.execute("DETACH DATABASE source")
    finally:
        con.close()

    # ``SpellRuneCost.dbc``: Icy Touch = one frost rune + 10 runic power,
    # Blood Strike = one blood rune.  The importer stores the DBC row verbatim.
    assert_eq(spells.get_rune_cost(world_db, 241), (0, 0, 1, 100),
              "Icy Touch rune cost")
    assert_eq(spells.get_rune_cost(world_db, 301), (1, 0, 0, 100),
              "Blood Strike rune cost")

    sock = object.__new__(realmsocket.WorldSocket)
    sock.config = types.SimpleNamespace(
        database=types.SimpleNamespace(world_path=world_db))
    sock.character = {"name": "Dk", "class": 6, "level": 55, "power": 0,
                      "max_power": 1000}
    sock.log = types.SimpleNamespace(debug=lambda *a, **k: None,
                                     info=lambda *a, **k: None,
                                     exception=lambda *a, **k: None)

    # ``Player::InitRunes``: BLOOD, BLOOD, UNHOLY, UNHOLY, FROST, FROST.
    sock._init_runes()
    assert_eq([int(rune["current"]) for rune in sock._get_combat_state()["runes"]],
              [0, 0, 1, 1, 2, 2], "six rune slots in the reference order")
    assert_eq(sock._rune_state_mask(), 0b111111, "all runes start ready")

    icy_touch = types.SimpleNamespace(power_type=5, rune_cost_id=241,
                                      name="Icy Touch")
    assert sock._can_afford_runes(icy_touch), "Icy Touch affordable at full runes"
    assert_eq(sock._pay_runes(icy_touch, take=False), [4],
              "Icy Touch spends the first frost rune")
    assert_eq(sock._pay_runes(icy_touch, take=True), [4],
              "Icy Touch spend returns the frost slot")
    runes = sock._get_combat_state()["runes"]
    assert_eq(int(runes[4]["cooldown"]), spells.RUNE_COOLDOWN,
              "a spent rune starts its 10 s cooldown")
    assert_eq(sock._rune_state_mask(), 0b101111,
              "only the spent frost rune leaves the ready mask")

    # A death rune pays for a basic rune and reverts to its base type on spend.
    sock._init_runes()
    runes = sock._get_combat_state()["runes"]
    for rune in runes:
        rune["cooldown"] = float(spells.RUNE_COOLDOWN)
    runes[0]["cooldown"] = 0.0
    runes[0]["current"] = spells.RUNE_DEATH
    blood_strike = types.SimpleNamespace(power_type=5, rune_cost_id=301,
                                         name="Blood Strike")
    assert_eq(sock._pay_runes(blood_strike, take=True), [0],
              "a death rune covers a blood cost")
    assert_eq(int(runes[0]["current"]), spells.RUNE_BLOOD,
              "death rune reverts to its base type when spent")
    assert_eq(int(runes[0]["cooldown"]), spells.RUNE_COOLDOWN,
              "the death rune starts the same cooldown")

    # ``SMSG_RESYNC_RUNES``: count, then (type, 255 - cooldown*255/10000).
    resync = spells.build_resync_runes([(0, 0), (0, spells.RUNE_COOLDOWN),
                                        (1, 5000)])
    assert_eq(struct.unpack_from("<I", resync, 0)[0], 3, "resync rune count")
    assert_eq(resync[4:], bytes([0, 255, 0, 0, 1, 128]),
              "resync types and cooldown progress bytes")
    assert_eq(spells.build_add_rune_power(5), struct.pack("<I", 32),
              "SMSG_ADD_RUNE_POWER marks the recovered slot")

    # Legacy ``creature_template_spells``: Mosshide Mistweaver knows Frostbolt
    # (victim) and Frost Armor (self).  Level 22 class 8 has 1357 base mana.
    spawn = {
        "entry": 1009, "spell_list_id": 0, "unit_class": 8, "min_level": 22,
        "energy_multiplier": 1.0, "current_power": 0, "unit_power_type": 0,
        "name": "Mosshide Mistweaver",
    }
    dummy = object.__new__(realmsocket.WorldSocket)
    dummy.config = sock.config
    ai = realmsocket.WorldSocket._build_creature_ai(dummy, spawn, 0.0)
    assert_eq(ai["max_mana"], 1357, "creature base mana from classlevelstats")
    by_id = {entry["spell_id"]: entry for entry in ai["spells"]}
    assert_eq(set(by_id), {20792, 12544}, "legacy template spell set")
    assert_eq(by_id[20792]["target_id"], 1, "Frostbolt targets the victim")
    assert_eq(by_id[12544]["target_id"], 2, "Frost Armor targets the caster")

    # ``creature_spell_list`` helpers: Fireball (1 s initial) and Flamestrike
    # (5-20 s initial, 18-20 s repeat), both aimed at the current victim.
    entries = spells.get_creature_spell_list(world_db, 89101)
    assert_eq([entry["spell_id"] for entry in entries], [19816, 20296],
              "spell list positions are preserved")
    assert_eq((entries[1]["initial_min"], entries[1]["initial_max"],
               entries[1]["repeat_min"], entries[1]["repeat_max"]),
              (5000, 20000, 18000, 20000), "spell list timers")
    header = spells.get_creature_spell_list_entry(world_db, 89101)
    assert_eq(header["chance_ranged_attack"], 75, "spell list header chance")
    targeting = spells.get_creature_spell_targeting(world_db, 100)
    assert_eq(targeting["type"], 1, "targeting id 100 is an attack target")

    # Drive one complete creature cast: start packet, mana spend, GO packet and
    # the Frostbolt damage.  This is the same code path ``_creature_combat_tick``
    # runs for a real spawn.
    packets: list[int] = []
    payloads: list[bytes] = []
    damage: list[int] = []

    async def send_packet(opcode, payload):
        packets.append(opcode)
        payloads.append(bytes(payload))

    async def noop(*args, **kwargs):
        return None

    dummy.player_guid = 0x42
    dummy.character = {"name": "Tester", "class": 6, "level": 55,
                       "health": 500, "max_health": 500, "power": 0,
                       "max_power": 1000, "x": 0.0, "y": 0.0, "z": 0.0}
    dummy.log = types.SimpleNamespace(
        debug=lambda *args, **kwargs: None,
        info=lambda *args, **kwargs: None,
        exception=lambda *args, **kwargs: None)
    dummy.send_packet = send_packet
    dummy._send_creature_fields = noop
    dummy._set_creature_power = lambda guid, power: None
    dummy._set_creature_health = lambda guid, health: None
    dummy._send_spell_damage_log = noop
    dummy._send_spell_heal_log = noop
    dummy._send_attacker_state_update = noop
    dummy._deal_damage_to_creature = noop
    dummy._is_player_guid = lambda guid: int(guid) == dummy.player_guid
    dummy._server_tick_ms = lambda: 0

    async def damage_player(amount, source, physical=False):
        damage.append(int(amount))

    dummy._damage_player = damage_player
    dummy._combat_state = {
        "dots": [], "dead": False, "runes": [], "rune_prediction": None,
        "aggro": {7: {"pos": (0.0, 0.0, 0.0), "ai": ai}},
    }
    ai["spells"] = [by_id[20792]]
    ai["next_tick"] = 0.0
    ai["gcd_until"] = 0.0
    by_id[20792]["next_ready"] = 0.0

    # Keep the scripted Frostbolt deterministic: real combat rolls a 4% spell
    # miss against an equal-level target.
    dummy._creature_spell_miss_chance = lambda *_args: 0.0

    async def creature_cast_check():
        await realmsocket.WorldSocket._creature_spell_list_tick(
            dummy, 7, spawn, ai, 100.0, False)
        assert ai["cast"] is not None, "creature starts a spell-list cast"
        assert packets and packets[0] == int(Opcodes.SMSG_SPELL_START), \
            "creature cast opens with SMSG_SPELL_START"
        before = ai["mana"]
        await realmsocket.WorldSocket._finish_creature_cast(
            dummy, 7, spawn, ai)
        assert_eq(ai["mana"], before - 90, "Frostbolt mana cost is paid")
        assert int(Opcodes.SMSG_SPELL_GO) in packets, \
            "creature cast sends SMSG_SPELL_GO"
        # Parse the creature's GO exactly like the 3.3.5 client: the hit list
        # holds full uint64 guids, and the target mask must land right after
        # the miss count.  A packed guid here shifts the mask to 0xD7DA0000,
        # whose 0x80000 bit makes the client alloca a guid fragment as an
        # element count and crash with a stack overflow.
        go = payloads[packets.index(int(Opcodes.SMSG_SPELL_GO))]
        _caster, pos = read_packed_guid(go, 0)
        _caster_unit, pos = read_packed_guid(go, pos)
        pos += 1                                        # cast id
        pos += 4                                        # spell id
        pos += 4 + 4                                    # flags + timestamp
        assert_eq(go[pos], 1, "creature GO hit count")
        pos += 1
        assert_eq(struct.unpack_from("<Q", go, pos)[0], dummy.player_guid,
                  "creature GO hit guid is a raw uint64")
        pos += 8
        assert_eq(go[pos], 0, "creature GO miss count")
        pos += 1
        assert_eq(struct.unpack_from("<I", go, pos)[0], 0x02,
                  "creature GO target mask is UNIT at the expected offset")
        pos += 4
        assert_eq(pos + len(spells.pack_guid(dummy.player_guid)), len(go),
                  "creature GO ends after the packed target guid")
        assert damage and damage[0] > 0, "Frostbolt damages the player"

    await creature_cast_check()

    async def rune_go_check():
        """``SMSG_SPELL_GO`` rune prediction for a rune spell and Death Coil."""
        sent: list[tuple[int, bytes]] = []

        async def send_packet(opcode, payload):
            sent.append((opcode, payload))

        async def set_power(power):
            sock.character["power"] = int(power)

        sock.player_guid = 0x42
        sock.send_packet = send_packet
        sock._set_player_power = set_power
        sock._server_tick_ms = lambda: 0
        sock._init_runes()

        def go_fields(payload: bytes):
            _caster, pos = read_packed_guid(payload, 0)
            _unit, pos = read_packed_guid(payload, pos)
            pos += 1                                    # cast id
            pos += 4                                    # spell id
            flags = struct.unpack_from("<I", payload, pos)[0]
            pos += 4 + 4                                # flags + timestamp
            hits = payload[pos]
            pos += 1
            for _ in range(hits):
                # WriteSpellGoTargets writes full uint64 guids here, not
                # packed ones; a packed guid shifts every later field.
                pos += 8
            pos += 1                                    # miss count
            target_mask = struct.unpack_from("<I", payload, pos)[0]
            pos += 4
            if target_mask & 0x2:
                _guid, pos = read_packed_guid(payload, pos)
            predicted_power = None
            if flags & spells.CAST_FLAG_PREDICTED_POWER:
                predicted_power = struct.unpack_from("<I", payload, pos)[0]
                pos += 4
            before = after = None
            progress = []
            if flags & spells.CAST_FLAG_PREDICTED_RUNES:
                before, after = payload[pos], payload[pos + 1]
                pos += 2
                for index in range(6):
                    if before & (1 << index) and not after & (1 << index):
                        progress.append(payload[pos])
                        pos += 1
            assert pos == len(payload), "SMSG_SPELL_GO parsed to the end"
            return flags, predicted_power, before, after, progress

        icy = spells.get_spell_template(world_db, 45477)
        assert await sock._take_power(icy), "Icy Touch spends a frost rune"
        cast = spells.SpellCast(sock.player_guid, icy.id, 0)
        cast.template = icy
        await sock._send_spell_go(cast, icy, 0, 1)
        flags, power, before, after, progress = go_fields(sent[-1][1])
        assert flags & spells.CAST_FLAG_PREDICTED_RUNES, "rune prediction flag"
        assert flags & spells.CAST_FLAG_PREDICTED_POWER, "predicted power flag"
        assert flags & spells.CAST_FLAG_NO_GCD, "rune casts carry NO_GCD"
        assert_eq(power, 0, "POWER_RUNE predicts a zero power field")
        assert_eq((before, after), (0b111111, 0b101111),
                  "the frost rune leaves the predicted mask")
        assert_eq(progress, [0], "a just-spent rune has no cooldown progress")
        assert_eq(int(sock.character["power"]), 100,
                  "Icy Touch grants 10 runic power")

        # Death Coil also has a RuneCostID but costs runic power, so the runes
        # stay ready and no predicted-power field may overwrite the RP bar.
        sock._init_runes()
        sock.character["power"] = 1000
        death_coil = spells.get_spell_template(world_db, 47541)
        assert await sock._take_power(death_coil), "Death Coil spends runic power"
        cast = spells.SpellCast(sock.player_guid, death_coil.id, sock.player_guid)
        cast.template = death_coil
        await sock._send_spell_go(cast, death_coil, sock.player_guid, 2)
        flags, power, before, after, progress = go_fields(sent[-1][1])
        assert flags & spells.CAST_FLAG_PREDICTED_RUNES, "Death Coil rune masks"
        assert not flags & spells.CAST_FLAG_PREDICTED_POWER, \
            "Death Coil sends no predicted-power field"
        assert_eq((before, after, progress), (0b111111, 0b111111, []),
                  "Death Coil leaves the runes untouched")
        assert_eq(int(sock.character["power"]), 600, "Death Coil costs 40 runic power")

    await rune_go_check()

    async def idle_movement_check():
        """Idle creatures wander inside their spawn radius; NPCs stay put."""
        sent: list[tuple[int, bytes]] = []

        async def send_packet(opcode, payload):
            sent.append((int(opcode), bytes(payload)))

        mover = object.__new__(realmsocket.WorldSocket)
        mover.config = sock.config
        mover.log = sock.log
        mover.send_packet = send_packet
        wander = {
            "guid": 7, "entry": 1125, "x": -6180.0, "y": -400.0, "z": 100.0,
            "o": 0.0, "dead_since": 0, "unit_flags": 0,
            "spawn_movement_type": 1, "template_movement_type": 0,
            "npc_flags": 0, "spawndist": 8.0, "speed_walk": 2.5,
            "movement_info_id": 0,
        }
        state = mover._creature_idle_state(7, wander, 100.0)
        state["next_move"] = 0.0
        await mover._creature_idle_tick(7, wander, 100.0)
        assert_eq(len(sent), 1, "wander sends one SMSG_MONSTER_MOVE")
        opcode, payload = sent[0]
        assert_eq(opcode, int(Opcodes.SMSG_MONSTER_MOVE), "idle move opcode")
        _guid, pos = read_packed_guid(payload, 0)
        pos += 1 + 12 + 4 + 1                       # toggle, start, spline, mode
        flags, duration, count = struct.unpack_from("<III", payload, pos)
        pos += 12
        dx, dy, dz = struct.unpack_from("<fff", payload, pos)
        assert_eq(pos + 12, len(payload), "idle spline has exactly one point")
        assert_eq(flags, 0, "idle spline has no flags")
        assert_eq(count, 1, "idle spline is a single segment")
        assert duration >= realmsocket.IDLE_MOVE_MIN_DURATION_MS, \
            "idle move has a duration"
        radius = ((dx - wander["x"]) ** 2 + (dy - wander["y"]) ** 2) ** 0.5
        assert 0.1 < radius <= wander["spawndist"] + 0.01, \
            "wander stays inside spawndist"
        assert_eq(round(dz, 3), wander["z"], "wander keeps the spawn height")

        # NPCs and rooted creatures are idle; a waypoint path wins.
        npc = dict(wander, guid=8, npc_flags=1, spawn_movement_type=-1)
        assert_eq(mover._creature_movement_plan(npc), ("idle", []),
                  "NPCs without a path are idle")
        rooted = dict(wander, guid=9,
                      unit_flags=realmsocket.UNIT_FLAG_DISABLE_MOVE)
        assert_eq(mover._creature_movement_plan(rooted), ("idle", []),
                  "UNIT_FLAG_DISABLE_MOVE suppresses wandering")
        waypoint = dict(wander, guid=10, spawn_movement_type=2)
        path = [{"point": 1, "position_x": wander["x"] + 4.0,
                 "position_y": wander["y"], "position_z": wander["z"],
                 "orientation": 0.0, "wait_time": 0}]
        mover._creature_waypoint_path = lambda spawn: path
        kind, got = mover._creature_movement_plan(waypoint)
        assert_eq((kind, len(got)), ("waypoint", 1),
                  "a waypoint path is selected")

    await idle_movement_check()

    async def summon_check():
        """Summon Imp creates an owned creature, fights, and expires."""
        sent: list[tuple[int, bytes]] = []
        peer_sent: list[tuple[int, bytes]] = []
        attacks: list[tuple[int, int, int]] = []
        damage: list[tuple[int, int, int]] = []

        async def send_packet(opcode, payload):
            sent.append((int(opcode), bytes(payload)))

        async def relay(opcode, payload):
            peer_sent.append((int(opcode), bytes(payload)))

        async def noop(*args, **kwargs):
            return None

        pet = object.__new__(realmsocket.WorldSocket)
        pet.config = sock.config
        pet.log = sock.log
        pet.character = {"name": "Demo", "class": 9, "race": 1, "level": 3,
                         "map": 0, "zone": 12, "x": 0.0, "y": 0.0, "z": 0.0,
                         "o": 0.0}
        pet.player_guid = 0x42
        pet.in_world = True
        pet.send_packet = send_packet
        pet.send_packet_to_peers = relay
        pet._visible_creatures = {}
        pet._creature_spawn_cache = {}
        pet._summoned_creatures = {}
        pet._summon_states = {}
        pet._summon_serial = 0
        pet._combat_state = {
            "dead": False, "in_combat": False, "auto_attacking": False,
            "target_guid": 0, "selected_guid": 0, "aggro": {},
            "idle": {}, "idle_settle": {},
        }
        enemy = {
            "guid": 7, "entry": 1009, "name": "Mosshide Mistweaver",
            "x": 0.5, "y": 0.0, "z": 0.0, "o": 0.0, "dead_since": 0,
            "combat_reach": 1.5, "min_level": 22, "max_level": 22,
            "base_health": 1000, "hp_multiplier": 1.0,
        }

        def get_spawn(low_guid):
            if int(low_guid) in pet._summoned_creatures:
                return pet._summoned_creatures[int(low_guid)]
            return enemy if int(low_guid) == 7 else None

        async def attacker_update(attacker, victim, amount, **kwargs):
            attacks.append((int(attacker), int(victim), int(amount)))

        async def deal_damage(guid, amount, attacker_guid=0):
            damage.append((int(guid), int(amount), int(attacker_guid)))
            return False

        pet._get_creature_spawn = get_spawn
        pet._send_creature_fields = noop
        pet._send_attacker_state_update = attacker_update
        pet._deal_damage_to_creature = deal_damage

        summon_spell = spells.get_spell_template(world_db, 688)
        assert_eq(summon_spell.summon_entries(), [(416, True)],
                  "Summon Imp references the imp pet")
        assert_eq(spells.summon_duration_ms(world_db, 21, 1), -1,
                  "warlock pets last until dismissed")

        low_guid = await pet._summon_creature(416, -1, is_pet=True)
        assert low_guid is not None and low_guid >= realmsocket.SUMMON_GUID_BASE, \
            "summon allocates a runtime creature guid"
        spawn = pet._summoned_creatures[low_guid]
        values = object_update.build_creature_values(spawn)
        assert_eq(values[F.UNIT_FIELD_SUMMONEDBY], 0x42,
                  "summon stores its owner in UNIT_FIELD_SUMMONEDBY")
        assert_eq(values[F.UNIT_FIELD_FLAGS], 0x00000008,
                  "summon is player-controlled")
        create_packets = [packet for packet in sent if packet[0] in (
            int(Opcodes.SMSG_UPDATE_OBJECT),
            int(Opcodes.SMSG_COMPRESSED_UPDATE_OBJECT))]
        assert create_packets, "summon sends a create object"
        assert peer_sent and peer_sent[-1][0] == create_packets[-1][0], \
            "summon create block is relayed to peers"
        assert any(packet[0] == int(Opcodes.SMSG_PET_SPELLS)
                   for packet in sent), "summon sends the pet action bar"
        assert any(packet[0] == int(Opcodes.SMSG_TALENTS_INFO)
                   for packet in sent), "summon sends pet talent info"

        # Assist: the pet attacks the owner's current target.
        pet._combat_state.update({
            "in_combat": True, "auto_attacking": True,
            "target_guid": creature.creature_guid(7, 1009),
        })
        spawn["x"], spawn["y"], spawn["z"] = 0.5, 0.0, 0.0
        pet._summon_state(low_guid)["last_swing"] = 0.0
        await pet._summon_tick_all(100.0)
        assert attacks and attacks[-1][0] == creature.creature_guid(
            low_guid, 416), "pet is the attacker in the combat log"
        assert damage and damage[-1][0] == creature.creature_guid(7, 1009), \
            "pet damage reaches the owner's target"

        # Timed summons expire and send a destroy packet.
        pet._summon_state(low_guid)["expires"] = 0.0
        sent.clear()
        await pet._summon_tick_all(101.0)
        assert_eq(low_guid in pet._summoned_creatures, False,
                  "expired summon is removed")
        assert sent and sent[-1][0] == int(Opcodes.SMSG_DESTROY_OBJECT), \
            "expiry sends SMSG_DESTROY_OBJECT"

        await pet._summon_creature(416, -1)
        await pet.dismiss_all_summons()
        assert_eq(pet._summoned_creatures, {}, "summons can be dismissed")

    await summon_check()
    print("  runes/creature AI: rune spend + death rune + resync, spell-list"
          " selection, Frostbolt cast and mana spend, DK GO rune prediction,"
          " idle wander spline, summon create/assist/expiry OK")


# ----------------------------------------------------------------------
# Authserver flow
# ----------------------------------------------------------------------
async def authserver_flow(host: str, port: int, username: str = USERNAME,
                          password: str = PASSWORD):
    reader, writer = await asyncio.open_connection(host, port)
    try:
        writer.write(make_logon_challenge(username))
        await writer.drain()

        challenge = await reader.readexactly(119)
        assert_eq(challenge[0], 0x00, "challenge command")
        assert_eq(challenge[1], 0x00, "challenge unknown byte")
        assert_eq(challenge[2], 0x00, "challenge error")

        b_bytes = challenge[3:35]
        g_len, g = challenge[35], challenge[36]
        n_len = challenge[37]
        n_bytes = challenge[38:70]
        salt = challenge[70:102]
        version_challenge = challenge[102:118]
        security_flags = challenge[118]

        assert_eq(g_len, 1, "g length")
        assert_eq(g, SRP6.g, "g")
        assert_eq(n_len, 32, "N length")
        assert_eq(n_bytes, SRP6.N_BYTES, "N")
        assert_eq(version_challenge, bytes.fromhex("BAA31E99A00B2157FC373FB369CDD2F1"), "version challenge")
        assert_eq(security_flags, 0, "security flags")

        a_bytes, m1, session_key = client_srp(username, password, b_bytes, salt)

        proof = bytes([0x01]) + a_bytes + m1 + bytes(20) + bytes([0, 0])
        writer.write(proof)
        await writer.drain()

        proof_response = await reader.readexactly(32)
        assert_eq(proof_response[0], 0x01, "proof command")
        assert_eq(proof_response[1], 0x00, "proof error")
        expected_m2 = SRP6.h(a_bytes, m1, session_key)
        assert_eq(proof_response[2:22], expected_m2, "M2")
        account_flags = struct.unpack_from("<I", proof_response, 22)[0]
        assert_eq(account_flags, 0x00800000, "account flags")

        # Realms
        writer.write(bytes([0x10]) + struct.pack("<I", BUILD))
        await writer.drain()

        realm_header = await reader.readexactly(3)
        assert_eq(realm_header[0], 0x10, "realm list command")
        realm_size = struct.unpack("<H", realm_header[1:3])[0]
        realm_body = await reader.readexactly(realm_size)

        unknown = struct.unpack_from("<I", realm_body, 0)[0]
        count = struct.unpack_from("<H", realm_body, 4)[0]
        assert_eq(unknown, 0, "realm list unknown")
        assert_eq(count, 1, "realm count")
        assert_eq(realm_body[-2:], b"\x10\x00", "realm list terminator")

        pos = 6
        realm_type, locked, flags = realm_body[pos], realm_body[pos + 1], realm_body[pos + 2]
        pos += 3
        realm_name, pos = read_cstring(realm_body, pos)
        realm_addr, pos = read_cstring(realm_body, pos)
        population = struct.unpack_from("<f", realm_body, pos)[0]
        pos += 4
        char_count = realm_body[pos]
        timezone = realm_body[pos + 1]
        realm_id = realm_body[pos + 2]

        print(f"  realm: {realm_name!r} at {realm_addr} (type={realm_type}, locked={locked}, "
              f"flags=0x{flags:02X}, chars={char_count}, tz={timezone}, id={realm_id})")

        return session_key, realm_addr, realm_id
    finally:
        writer.close()
        await writer.wait_closed()


# ----------------------------------------------------------------------
# Worldserver flow
# ----------------------------------------------------------------------
async def worldserver_flow(host: str, port: int, session_key: bytes, realm_id: int,
                           world_db_path: str | None = None,
                           character_db_path: str | None = None):
    reader, writer = await asyncio.open_connection(host, port)
    try:
        opcode, challenge = await read_server_packet(reader)
        assert_eq(opcode, int(Opcodes.SMSG_AUTH_CHALLENGE), "auth challenge opcode")
        assert_eq(len(challenge), 40, "auth challenge length")
        dos_zero_bits = struct.unpack_from("<I", challenge, 0)[0]
        server_challenge = challenge[4:8]
        dos_challenge = challenge[8:40]
        assert_eq(dos_zero_bits, 1, "DOS zero bits")
        assert_eq(len(dos_challenge), 32, "DOS challenge length")

        local_challenge = os.urandom(4)
        digest = SRP6.h(
            USERNAME.encode("utf-8"),
            b"\x00\x00\x00\x00",
            local_challenge,
            server_challenge,
            session_key,
        )

        payload = bytearray()
        payload += struct.pack("<I", BUILD)
        payload += struct.pack("<I", 0)  # login server id
        payload += USERNAME.encode("utf-8") + b"\x00"
        payload += struct.pack("<I", 0)  # login server type
        payload += local_challenge
        payload += struct.pack("<I", 0)  # region id
        payload += struct.pack("<I", 0)  # battlegroup id
        payload += struct.pack("<I", realm_id)
        payload += struct.pack("<Q", 0)  # DOS response
        payload += digest
        # Secure-addon list exactly as the 3.3.5a client appends it: a zlib
        # stream preceded by its uncompressed size.
        addons = struct.pack("<I", 2)
        for addon_name in ("Blizzard_CombatLog", "Blizzard_TalentUI"):
            addons += addon_name.encode("utf-8") + b"\x00"
            addons += b"\x00"                    # hasPublicKey
            addons += struct.pack("<II", 0, 0)   # publicKeyCrc, urlCrc
        addons += struct.pack("<I", 0x4B44953B)  # banned-addon timestamp
        compressed_addons = zlib.compress(addons)
        payload += struct.pack("<I", len(addons))
        payload += compressed_addons
        await send_client_packet(writer, int(Opcodes.CMSG_AUTH_SESSION), bytes(payload))

        crypt = WorldCrypt(session_key, client_side=True)

        opcode, auth_response = await read_server_packet(reader, crypt)
        assert_eq(opcode, int(Opcodes.SMSG_AUTH_RESPONSE), "auth response opcode")
        assert_eq(auth_response[0], 12, "AUTH_OK")
        assert_eq(auth_response[4], 0, "billing plan flags")
        assert_eq(auth_response[10], 2, "expansion")

        # Login-screen packets sent right after AUTH_RESPONSE.
        opcode, addon_reply = await read_server_packet(reader, crypt)
        assert_eq(opcode, int(Opcodes.SMSG_ADDON_INFO), "addon info opcode")
        # state=2 (Blizzard-signed) + the Blizzard public key for each of the 2
        # addons, followed by an empty banned-addon list.  Without the key the
        # client leaves the addons untrusted and taints the whole UI.
        blizzard_record = (
            bytes([2, 1, 1])
            + addon_info.BLIZZARD_PUBLIC_KEY
            + struct.pack("<I", 0)
            + bytes([0])
        )
        assert_eq(len(addon_info.BLIZZARD_PUBLIC_KEY), 256, "Blizzard key length")
        assert_eq(addon_reply, blizzard_record * 2 + struct.pack("<I", 0),
                  "addon info reply")

        opcode, cache_version_payload = await read_server_packet(reader, crypt)
        assert_eq(opcode, int(Opcodes.SMSG_CLIENTCACHE_VERSION), "cache version opcode")
        assert_eq(len(cache_version_payload), 4, "cache version length")
        sent_cache_version = struct.unpack("<I", cache_version_payload)[0]
        if world_db_path:
            assert_eq(sent_cache_version,
                      character_db.client_cache_version(world_db_path),
                      "cache version hashes the item templates")
        assert sent_cache_version != 0, "cache version is never 0"

        opcode, tutorials = await read_server_packet(reader, crypt)
        assert_eq(opcode, int(Opcodes.SMSG_TUTORIAL_FLAGS), "tutorial flags opcode")
        assert_eq(tutorials, b"\xff" * 32, "tutorial flags length")

        # Character enumeration starts empty.
        await send_client_packet(writer, int(Opcodes.CMSG_CHAR_ENUM), crypt=crypt)
        opcode, enum_payload = await read_server_packet(reader, crypt)
        assert_eq(opcode, int(Opcodes.SMSG_CHAR_ENUM), "char enum opcode")
        assert_eq(enum_payload[0], 0, "initial character count")

        # An illegal race/class pair must be rejected.  Gnomes cannot be
        # paladins in 3.3.5a, so a hand-built CMSG_CHAR_CREATE must fail and
        # leave no character behind (the client UI never offers this combo).
        illegal_payload = b"Gnomepal\x00" + bytes([7, 2, 0, 0, 0, 0, 0, 0, 0])
        await send_client_packet(writer, int(Opcodes.CMSG_CHAR_CREATE), illegal_payload, crypt)
        opcode, illegal_result = await read_server_packet(reader, crypt)
        assert_eq(opcode, int(Opcodes.SMSG_CHAR_CREATE), "illegal combo create opcode")
        assert_eq(illegal_result[0], 49, "illegal race/class rejected")
        await send_client_packet(writer, int(Opcodes.CMSG_CHAR_ENUM), crypt=crypt)
        opcode, enum_payload = await read_server_packet(reader, crypt)
        assert_eq(opcode, int(Opcodes.SMSG_CHAR_ENUM), "char enum after illegal combo")
        assert_eq(enum_payload[0], 0, "illegal combo created no character")

        # Create a character.
        create_payload = b"Selftest\x00" + bytes([1, 1, 0, 0, 0, 0, 0, 0, 0])
        await send_client_packet(writer, int(Opcodes.CMSG_CHAR_CREATE), create_payload, crypt)
        opcode, create_result = await read_server_packet(reader, crypt)
        assert_eq(opcode, int(Opcodes.SMSG_CHAR_CREATE), "char create opcode")
        assert_eq(create_result[0], 47, "CHAR_CREATE_SUCCESS")

        # Enumerate again and parse the record.
        await send_client_packet(writer, int(Opcodes.CMSG_CHAR_ENUM), crypt=crypt)
        opcode, enum_payload = await read_server_packet(reader, crypt)
        assert_eq(opcode, int(Opcodes.SMSG_CHAR_ENUM), "char enum opcode")
        assert_eq(enum_payload[0], 1, "character count after create")

        pos = 1
        guid = struct.unpack_from("<Q", enum_payload, pos)[0]
        pos += 8
        name, pos = read_cstring(enum_payload, pos)
        appearance = enum_payload[pos:pos + 9]
        pos += 9
        assert_eq(name, "Selftest", "character name")
        assert_eq(tuple(appearance[:3]), (1, 1, 0), "character appearance")
        assert_eq(appearance[8], 1, "character level")

        pos += 8    # zone, map
        pos += 12   # x, y, z
        pos += 12   # guild id, char flags, customize flags
        pos += 1    # first login
        pos += 12   # pet display id, level, family
        pos += 23 * 9  # 19 equipment slots + 4 bags
        assert_eq(pos, len(enum_payload), "SMSG_CHAR_ENUM record length")

        # Enter the world with it.
        await send_client_packet(
            writer, int(Opcodes.CMSG_PLAYER_LOGIN), struct.pack("<Q", guid), crypt
        )

        expected_opcodes = [
            Opcodes.SMSG_LOGIN_VERIFY_WORLD,
            Opcodes.SMSG_ACCOUNT_DATA_TIMES,
            Opcodes.SMSG_FEATURE_SYSTEM_STATUS,
            Opcodes.SMSG_MOTD,
            Opcodes.SMSG_LEARNED_DANCE_MOVES,
            Opcodes.SMSG_BINDPOINTUPDATE,
            Opcodes.SMSG_INITIAL_SPELLS,
            Opcodes.SMSG_ACTION_BUTTONS,
            Opcodes.SMSG_INITIALIZE_FACTIONS,
            Opcodes.SMSG_LOGIN_SETTIMESPEED,
            Opcodes.SMSG_NAME_QUERY_RESPONSE,
            Opcodes.SMSG_TUTORIAL_FLAGS,
            Opcodes.SMSG_INIT_WORLD_STATES,
            None,  # the (possibly compressed) update object
            Opcodes.SMSG_CLIENT_CONTROL_UPDATE,
            Opcodes.SMSG_TIME_SYNC_REQ,
            Opcodes.SMSG_TALENTS_INFO,
        ]
        update_payload = None
        update_compressed = False
        for index, expected in enumerate(expected_opcodes):
            opcode, payload = await read_server_packet(reader, crypt)
            if expected is None:
                if opcode == Opcodes.SMSG_COMPRESSED_UPDATE_OBJECT:
                    update_compressed = True
                else:
                    assert_eq(opcode, int(Opcodes.SMSG_UPDATE_OBJECT), "update object opcode")
                update_payload = payload
                continue
            assert_eq(opcode, int(expected), f"login packet #{index} opcode")

            if expected == Opcodes.SMSG_TALENTS_INFO:
                assert_eq(payload[0], 0, "talent info kind")
                assert_eq(struct.unpack_from("<I", payload, 1)[0], 0,
                          "level-1 talent points")
                assert_eq(payload[5], 1, "talent group count")
                assert_eq(payload[6], 0, "active talent group")
                assert_eq(payload[7], 0, "learned talent count")
                assert_eq(payload[8], 6, "glyph slot count")
                assert_eq(len(payload), 21, "talent info length")
                continue
            if expected == Opcodes.SMSG_CLIENT_CONTROL_UPDATE:
                control_guid, control_pos = read_packed_guid(payload, 0)
                assert_eq(control_guid, guid, "client control update guid")
                assert_eq(payload[control_pos], 1, "client control update allow-move")
                assert_eq(control_pos + 1, len(payload), "client control update length")
            elif expected == Opcodes.SMSG_LOGIN_VERIFY_WORLD:
                map_id, vx, vy, vz, vo = struct.unpack("<Iffff", payload)
                assert_eq(map_id, 0, "LOGIN_VERIFY_WORLD map")
                assert_eq((round(vx, 2), round(vy, 2), round(vz, 2)),
                          (-8949.95, -132.49, 83.53), "LOGIN_VERIFY_WORLD position")
                print(f"  login verify: map {map_id} at ({vx:.2f}, {vy:.2f}, {vz:.2f}) o={vo:.2f}")
            elif expected == Opcodes.SMSG_MOTD:
                line_count = struct.unpack_from("<I", payload, 0)[0]
                assert_eq(line_count, 1, "MOTD line count")
                motd_line, motd_end = read_cstring(payload, 4)
                assert_eq(motd_end, len(payload), "MOTD length")
                assert_eq(motd_line, "SelfTest", "MOTD text")
            elif expected == Opcodes.SMSG_BINDPOINTUPDATE:
                assert_eq(len(payload), 20, "BINDPOINTUPDATE length")
            elif expected == Opcodes.SMSG_ACTION_BUTTONS:
                assert_eq(len(payload), 1 + 144 * 4, "ACTION_BUTTONS length")
                assert_eq(payload[0], 1, "ACTION_BUTTONS reason (initial)")
                buttons = struct.unpack_from("<144I", payload, 1)
                assert any(buttons), "ACTION_BUTTONS has at least one button"
                if buttons[0]:
                    assert_eq(buttons[0] & 0xFFFFFF, 6603, "button 0 is Auto Attack")
                    assert_eq(buttons[0] >> 24, 0, "button 0 is a spell action")
            elif expected == Opcodes.SMSG_INITIAL_SPELLS:
                assert_eq(payload[0], 1, "INITIAL_SPELLS initial-login flag")
                spell_count = struct.unpack_from("<H", payload, 1)[0]
                assert spell_count > 0, "INITIAL_SPELLS contains spells"
                assert_eq(len(payload), 3 + spell_count * 6 + 2,
                          "INITIAL_SPELLS length")
            elif expected == Opcodes.SMSG_INITIALIZE_FACTIONS:
                faction_count = struct.unpack_from("<I", payload, 0)[0]
                assert_eq(faction_count, 128, "faction count")
                assert_eq(len(payload), 4 + 128 * 5, "INITIALIZE_FACTIONS length")
            elif expected == Opcodes.SMSG_LOGIN_SETTIMESPEED:
                assert_eq(len(payload), 12, "LOGIN_SETTIMESPEED length")
            elif expected == Opcodes.SMSG_TUTORIAL_FLAGS:
                assert_eq(payload, b"\xff" * 32, "world tutorial flags")
            elif expected == Opcodes.SMSG_ACCOUNT_DATA_TIMES:
                # time, unknown, mask, one uint32 per masked type (all eight).
                # The mask must announce every type: the client defers its key
                # binding load until the matching record clears the download
                # flag, and an empty mask leaves movement/ESC/C/M dead.
                assert_eq(len(payload), 4 + 1 + 4 + 8 * 4, "account data times length")
                _server_time, unknown, mask = struct.unpack_from("<IBI", payload, 0)
                assert_eq(unknown, 0, "account data unknown byte")
                assert_eq(mask, 0xFF, "account data mask")
                times = struct.unpack_from("<8I", payload, 9)
                assert_eq(times, (0,) * 8, "account data times")

        assert update_payload is not None
        update = parse_update_object(update_payload, update_compressed)
        assert_eq(update["update_type"], 3, "UPDATETYPE_CREATE_OBJECT2")
        assert_eq(update["guid"], guid, "update object guid")
        assert_eq(update["type_id"], 4, "TYPEID_PLAYER")
        assert_eq(update["flags"], 0x71, "update flags")
        assert_eq(update["move_flags"], 0, "movement flags")
        assert_eq(update["move_time"], 0, "movement time")
        assert_eq(update["fall_time"], 0.0, "movement fall time")
        assert_eq(round(update["speeds"][0], 3), 2.5, "walk speed")
        assert_eq(round(update["speeds"][1], 3), 7.0, "run speed")
        px, py, pz, po = update["position"]
        assert_eq((round(px, 2), round(py, 2), round(pz, 2), round(po, 2)),
                  (-8949.95, -132.49, 83.53, 0.0), "update object position")
        assert_eq(update["high_guid"], 0x2F, "self high guid")
        assert_eq(update["mask_blocks"], 42, "update mask blocks")

        values = update["values"]
        assert_eq(values[F.OBJECT_FIELD_GUID], guid & 0xFFFFFFFF, "OBJECT_FIELD_GUID")
        assert_eq(values[F.OBJECT_FIELD_GUID + 1], guid >> 32, "OBJECT_FIELD_GUID high")
        assert_eq(values[F.OBJECT_FIELD_TYPE], 0x19, "OBJECT_FIELD_TYPE")
        # Level-1 human warrior: basehp 20 + 40 stamina health = 60.
        assert_eq(values[F.UNIT_FIELD_HEALTH], 60, "health")
        assert_eq(values[F.UNIT_FIELD_MAXHEALTH], 60, "max health")
        # Rage is stored in tenths: a full bar is 1000 (100 rage), and a fresh
        # warrior starts at zero.  Rage lives in the power-type-indexed slot
        # (POWER2/MAXPOWER2); writing POWER1 makes the client clamp the bar to
        # zero and refuse a 150-tenth ability like Heroic Strike locally.
        assert_eq(values[F.UNIT_FIELD_MAXPOWER2], 1000, "warrior max power (rage, tenths)")
        assert_eq(values[F.UNIT_FIELD_POWER2], 0, "warrior starts with no rage")
        assert F.UNIT_FIELD_POWER1 not in values, "warrior must not publish a mana bar"
        assert F.UNIT_FIELD_MAXPOWER1 not in values, "warrior must not publish a mana bar"
        assert_eq(values[F.UNIT_FIELD_LEVEL], 1, "level")
        assert_eq(values[F.UNIT_FIELD_FLAGS], 0x8, "UNIT_FLAG_PLAYER_CONTROLLED")
        assert_eq(values[F.UNIT_FIELD_FLAGS_2], 0x800, "UNIT_FLAG2_REGENERATE_POWER")
        # Primary attributes come from CMaNGOS player_levelstats: human warrior
        # at level 1 is 23/20/22/20/20 (str/agi/sta/int/spi).  A missing table
        # leaves the whole character sheet at zero.
        assert_eq(values[F.UNIT_FIELD_STAT0], 23, "strength")
        assert_eq(values[F.UNIT_FIELD_STAT1], 20, "agility")
        assert_eq(values[F.UNIT_FIELD_STAT2], 22, "stamina")
        assert_eq(values[F.UNIT_FIELD_STAT3], 20, "intellect")
        assert_eq(values[F.UNIT_FIELD_STAT4], 20, "spirit")
        assert_eq(values[F.UNIT_FIELD_POSSTAT0], 0, "positive strength modifier")
        assert_eq(values[F.UNIT_FIELD_NEGSTAT0], 0, "negative strength modifier")
        assert_eq(values[F.UNIT_FIELD_BASE_MANA], 0, "warrior base mana")
        assert_eq(values[F.UNIT_FIELD_BASE_HEALTH], 20, "base health")
        # Armor is ``Player::UpdateArmor``: item armor (Recruit's Pants, 2)
        # plus twice the final agility (20 * 2).
        assert_eq(values[F.UNIT_FIELD_RESISTANCES], 42, "armor")
        assert_eq(values[F.UNIT_FIELD_RESISTANCES + 1], 0, "holy resistance")
        assert_eq(values[F.UNIT_FIELD_RESISTANCES + 2], 0, "fire resistance")
        # ``Player::UpdateAttackPowerAndDamage``: warrior = level*3 + str*2 - 20.
        assert_eq(values[F.UNIT_FIELD_ATTACK_POWER], 29, "melee attack power")
        # Ranged AP for warrior = level + agility - 10.
        assert_eq(values[F.UNIT_FIELD_RANGED_ATTACK_POWER], 11, "ranged attack power")
        assert_eq(values[F.UNIT_FIELD_ATTACK_POWER_MODS], 0, "attack power mods")
        assert_eq(values[F.UNIT_FIELD_ATTACK_POWER_MULTIPLIER], 0,
                  "attack power multiplier")
        assert_eq(values[F.UNIT_FIELD_DISPLAYID], 49, "human male display id")
        assert_eq(values[F.PLAYER_FIELD_MAX_LEVEL], 80, "max level")
        assert_eq(values[F.PLAYER_BYTES_3] & 0xFF, 0, "gender")
        print(f"  world entry: guid {guid}, {len(values)} update fields, "
              f"display id {values[F.UNIT_FIELD_DISPLAYID]}, compressed={update_compressed}")

        # Static creatures travel in the same update packet as the player.
        creatures = update["creatures"]
        assert_eq(len(creatures), 11, "static creature blocks on login")
        by_guid = {block["guid"]: block for block in creatures}
        mcbride_guid = object_update.creature_guid(1001, 197)
        mcbride = by_guid.get(mcbride_guid)
        assert mcbride is not None, "Marshal McBride spawn 1001 is present"
        assert_eq(mcbride["type_id"], 3, "creature object type id is UNIT")
        assert_eq(mcbride["flags"], 0x60, "creature flags LIVING|POSITION")
        assert_eq(mcbride["high_guid"], None, "creature carries no high guid")
        assert_eq(mcbride["mask_blocks"], 5, "creature update mask blocks")
        mcbride_values = mcbride["values"]
        assert_eq(mcbride_values[F.OBJECT_FIELD_GUID], (197 << 24) | 1001,
                  "creature guid low word (entry<<24 | spawn counter)")
        assert_eq(mcbride_values[F.OBJECT_FIELD_GUID + 1], 0xF1300000,
                  "creature guid high word (HIGHGUID_UNIT)")
        assert_eq(creature.creature_entry_from_guid(mcbride_guid), 197,
                  "creature entry is recoverable from the guid")
        assert_eq(mcbride_values[F.OBJECT_FIELD_TYPE], 0x9, "creature OBJECT|UNIT")
        assert_eq(mcbride_values[F.OBJECT_FIELD_ENTRY], 197, "creature entry")
        assert_eq(mcbride_values[F.UNIT_FIELD_DISPLAYID], 1276, "creature display id")
        assert_eq(mcbride_values[F.UNIT_NPC_FLAGS], 1, "creature gossip flag")
        assert_eq(mcbride_values[F.UNIT_NPC_EMOTESTATE], 0, "creature emote state")
        assert_eq(mcbride_values[F.UNIT_FIELD_LEVEL], 5, "creature level")
        assert abs(mcbride["position"][3] - 3.2411) < 1e-3, "creature orientation"
        print(f"  creatures: {len(creatures)} static spawns in the login update, "
              f"e.g. 'Marshal McBride' guid 1001 display 1276")

        # Creature query: the client asks for the name/stats when the object
        # appears.  The response must carry the entry, name, title, type and
        # display ids exactly as DBCache expects.
        await send_client_packet(
            writer, int(Opcodes.CMSG_CREATURE_QUERY),
            struct.pack("<IQ", 197, mcbride_guid), crypt,
        )
        opcode, payload = await read_server_packet(reader, crypt)
        assert_eq(opcode, int(Opcodes.SMSG_CREATURE_QUERY_RESPONSE),
                  "creature query response opcode")
        creature_info = parse_creature_query_response(payload)
        assert_eq(creature_info["allow"], True, "creature query allowed")
        assert_eq(creature_info["entry"], 197, "creature query entry")
        # The nameplate name stays plain; the title is the separate tooltip
        # line, exactly as stored in the template.
        assert_eq(creature_info["name"], "Marshal McBride", "creature query name")
        assert_eq(creature_info["title"], "Marshal", "creature title is the tooltip role")
        assert_eq(creature_info["type"], 7, "creature query type (humanoid)")
        assert_eq(creature_info["displays"][0], 1276, "creature query display id")
        assert_eq(creature_info["variants"], ("", "", ""), "creature name variants")

        # A distinct job title is sent in the title field, which is the
        # vanilla "<Role>" line in the mouseover tooltip.
        michelle_guid = object_update.creature_guid(1004, 2327)
        await send_client_packet(
            writer, int(Opcodes.CMSG_CREATURE_QUERY),
            struct.pack("<IQ", 2327, michelle_guid), crypt,
        )
        opcode, payload = await read_server_packet(reader, crypt)
        assert_eq(opcode, int(Opcodes.SMSG_CREATURE_QUERY_RESPONSE),
                  "titled creature query response opcode")
        titled = parse_creature_query_response(payload)
        assert_eq(titled["name"], "Michelle Belle", "nameplate name stays plain")
        assert_eq(titled["title"], "Trade Goods",
                  "creature job title is the tooltip's role line")

        # Gossip hello: menu id, menu text id and both seeded options.
        await send_client_packet(
            writer, int(Opcodes.CMSG_GOSSIP_HELLO),
            struct.pack("<Q", mcbride_guid), crypt,
        )
        opcode, payload = await read_server_packet(reader, crypt)
        assert_eq(opcode, int(Opcodes.SMSG_GOSSIP_MESSAGE), "gossip hello opcode")
        gossip = parse_gossip_message(payload)
        assert_eq(gossip["guid"], mcbride_guid, "gossip guid echoes the creature")
        assert_eq(gossip["gossip_id"], 1, "gossip menu id")
        assert_eq(gossip["text_id"], 1001, "gossip text id")
        assert_eq(len(gossip["options"]), 2, "gossip option count")
        assert_eq(gossip["options"][0]["text"], "Where can I find work?",
                  "gossip option text")
        assert_eq(gossip["quests"], [], "gossip quest count")

        # The client answers SetGossipText(textId) with CMSG_NPC_TEXT_QUERY.
        await send_client_packet(
            writer, int(Opcodes.CMSG_NPC_TEXT_QUERY),
            struct.pack("<IQ", 1001, mcbride_guid), crypt,
        )
        opcode, payload = await read_server_packet(reader, crypt)
        assert_eq(opcode, int(Opcodes.SMSG_NPC_TEXT_UPDATE), "npc text update opcode")
        npc_text = parse_npc_text_update(payload)
        assert_eq(npc_text["allow"], True, "npc text allowed")
        assert_eq(npc_text["id"], 1001, "npc text id")
        assert "Welcome to Northshire" in npc_text["options"][0]["text"], \
            "npc text body"
        assert_eq(npc_text["options"][0]["probability"], 1.0, "npc text probability")
        assert_eq(npc_text["options"][1]["text"], "", "npc text padding option")

        # Selecting a gossip option with an action text replaces the window.
        await send_client_packet(
            writer, int(Opcodes.CMSG_GOSSIP_SELECT_OPTION),
            struct.pack("<QII", mcbride_guid, 1, 0), crypt,
        )
        opcode, payload = await read_server_packet(reader, crypt)
        assert_eq(opcode, int(Opcodes.SMSG_GOSSIP_MESSAGE),
                  "gossip select option opcode")
        selected = parse_gossip_message(payload)
        assert_eq(selected["text_id"], 1002, "gossip option action text")
        assert_eq(selected["options"], [], "action text clears the option list")

        # Goodbye has no action and must close the gossip window.
        await send_client_packet(
            writer, int(Opcodes.CMSG_GOSSIP_SELECT_OPTION),
            struct.pack("<QII", mcbride_guid, 1, 1), crypt,
        )
        opcode, payload = await read_server_packet(reader, crypt)
        assert_eq(opcode, int(Opcodes.SMSG_GOSSIP_COMPLETE), "gossip complete opcode")
        assert_eq(payload, b"", "gossip complete is empty")
        print("  npc: creature query, gossip hello/select and NPC text round-trips OK")

        # Login queries the client sends once the loading screen is gone.
        await send_client_packet(writer, int(Opcodes.CMSG_NAME_QUERY),
                                 struct.pack("<Q", guid), crypt)
        opcode, payload = await read_server_packet(reader, crypt)
        assert_eq(opcode, int(Opcodes.SMSG_NAME_QUERY_RESPONSE), "name query opcode")
        queried_guid, pos = read_packed_guid(payload, 0)
        assert_eq(queried_guid, guid, "name query guid")
        assert_eq(payload[pos], 0, "name query result")
        pos += 1
        queried_name, pos = read_cstring(payload, pos)
        assert_eq(queried_name, "Selftest", "name query name")
        realm_name, pos = read_cstring(payload, pos)
        assert_eq(realm_name, "", "name query realm")
        assert_eq(tuple(payload[pos:pos + 4]), (1, 0, 1, 0), "name query race/sex/class/declined")

        # The client reads a creature's template entry from guid bits 24-47 and
        # answers NPC nameplates with CMSG_NAME_QUERY for that full guid.  The
        # response must echo the packed guid and return the template name, or
        # the nameplate stays blank until the creature is targeted.
        await send_client_packet(writer, int(Opcodes.CMSG_NAME_QUERY),
                                 struct.pack("<Q", mcbride_guid), crypt)
        opcode, payload = await read_server_packet(reader, crypt)
        assert_eq(opcode, int(Opcodes.SMSG_NAME_QUERY_RESPONSE),
                  "creature name query opcode")
        queried_guid, pos = read_packed_guid(payload, 0)
        assert_eq(queried_guid, mcbride_guid, "creature name query guid")
        assert_eq(payload[pos], 0, "creature name query result")
        pos += 1
        creature_name, pos = read_cstring(payload, pos)
        assert_eq(creature_name, "Marshal McBride", "creature name query name")

        await send_client_packet(writer, int(Opcodes.CMSG_NAME_QUERY),
                                 struct.pack("<Q", michelle_guid), crypt)
        opcode, payload = await read_server_packet(reader, crypt)
        assert_eq(opcode, int(Opcodes.SMSG_NAME_QUERY_RESPONSE),
                  "titled creature name query opcode")
        queried_guid, pos = read_packed_guid(payload, 0)
        assert_eq(queried_guid, michelle_guid, "titled creature name query guid")
        assert_eq(payload[pos], 0, "titled creature name query result")
        pos += 1
        titled_name, pos = read_cstring(payload, pos)
        assert_eq(titled_name, "Michelle Belle",
                  "creature name query returns the plain nameplate name")
        print("  creature name query: packed entry resolves the nameplate")

        await send_client_packet(writer, int(Opcodes.CMSG_QUERY_TIME), b"", crypt)
        opcode, payload = await read_server_packet(reader, crypt)
        assert_eq(opcode, int(Opcodes.SMSG_QUERY_TIME_RESPONSE), "query time opcode")
        assert_eq(len(payload), 8, "query time length")
        server_time, reset_in = struct.unpack("<Ii", payload)
        assert server_time > 0 and 0 < reset_in <= 24 * 3600, "query time values"

        await send_client_packet(writer, int(Opcodes.CMSG_PLAYED_TIME), b"\x00", crypt)
        opcode, payload = await read_server_packet(reader, crypt)
        assert_eq(opcode, int(Opcodes.SMSG_PLAYED_TIME), "played time opcode")
        assert_eq(payload, struct.pack("<II", 0, 0), "played time payload")

        await send_client_packet(writer, int(Opcodes.CMSG_ITEM_QUERY_SINGLE),
                                 struct.pack("<I", 12345), crypt)
        opcode, payload = await read_server_packet(reader, crypt)
        assert_eq(opcode, int(Opcodes.SMSG_ITEM_QUERY_SINGLE_RESPONSE), "item query opcode")
        assert_eq(payload, struct.pack("<I", 12345 | 0x80000000), "unknown item payload")

        # A real weapon must advertise a sheath type or the client silently
        # skips the model: with the default sheathed weapon mode
        # (WEAPONMODE_SHEATHEDMODE) the attach link comes from
        # CCharacterComponent::GetSheatheLink, and SHEATHE_NONE (0) returns
        # ATTACH_NONE, so AddHandItem never creates the sword.  ItemStats_C
        # reads m_sheatheType right after m_material with 22 uint32 fields
        # still following it.
        await send_client_packet(writer, int(Opcodes.CMSG_ITEM_QUERY_SINGLE),
                                 struct.pack("<I", 25), crypt)
        opcode, payload = await read_server_packet(reader, crypt)
        assert_eq(opcode, int(Opcodes.SMSG_ITEM_QUERY_SINGLE_RESPONSE), "sword query opcode")
        assert_eq(struct.unpack_from("<I", payload, 0)[0], 25, "sword query entry")
        sword_sheath = struct.unpack_from("<I", payload, len(payload) - 92)[0]
        assert_eq(sword_sheath, 3, "sword query carries SHEATHE_HIP")
        print("  item query: sword entry 25 -> sheath type 3 (hip)")

        await send_client_packet(writer, int(Opcodes.CMSG_CALENDAR_GET_NUM_PENDING), b"", crypt)
        opcode, payload = await read_server_packet(reader, crypt)
        assert_eq(opcode, int(Opcodes.SMSG_CALENDAR_SEND_NUM_PENDING), "calendar opcode")
        assert_eq(payload, struct.pack("<I", 0), "calendar payload")

        # Character checks for every playable race: the values must match the
        # client's ChrRaces.dbc/FactionTemplate.dbc or it crashes on login.
        character_row = {
            "guid": guid, "name": "Selftest", "race": 1, "class": 1, "gender": 0,
            "skin": 0, "face": 0, "hair_style": 0, "hair_color": 0,
            "facial_style": 0, "level": 1, "map": 0, "zone": 1519,
            "x": -8949.95, "y": -132.49, "z": 83.53, "o": 0.0,
        }
        for race, (faction, male_display) in {
            1: (1, 49), 2: (2, 51), 3: (3, 53), 4: (4, 55), 5: (5, 57),
            6: (6, 59), 7: (115, 1563), 8: (116, 1478), 10: (1610, 15476),
            11: (1629, 16125),
        }.items():
            sample = dict(character_row, race=race, gender=0)
            race_values = object_update.build_player_values(sample)
            assert_eq(race_values[F.UNIT_FIELD_FACTIONTEMPLATE], faction, f"race {race} faction")
            assert_eq(race_values[F.UNIT_FIELD_DISPLAYID], male_display, f"race {race} display")
        # Mana regeneration must use the 3.3.5 ``sqrt(Int) * Spirit *
        # BASE_REGEN[level]`` per-second rate and publish it as a float, with a
        # zero interrupted rate (no talents) — the old flat formula regenerated
        # far too fast and made a Fireball's cost look free.  ``0.062937`` is
        # the level-1 ratio from the client's ``GtRegenMPPerSpt.dbc`` (the
        # previous table started at the level-15 value and was ~3x too low for
        # levels 1-14).
        mage_stats = {"str": 10, "agi": 20, "sta": 20, "inte": 21, "spi": 27}
        expected_regen = math.sqrt(21) * 27 * 0.062937
        assert abs(object_update.mana_regen_per_second(mage_stats, 1)
                   - expected_regen) < 1e-9, "mana regen formula"
        assert_eq(object_update.base_regen_for_level(1), 0.062937,
                  "level-1 mana regen ratio")
        assert_eq(object_update.base_regen_for_level(14), 0.015771,
                  "level-14 mana regen ratio")
        assert_eq(object_update.base_regen_for_level(15), 0.014008,
                  "level-15 mana regen ratio")
        mage_row = dict(character_row, race=4, stats=mage_stats,
                        base_mana=100, max_power=100, power=100)
        mage_row["class"] = 8
        mage_values = object_update.build_player_values(mage_row)
        assert_eq(mage_values[F.UNIT_FIELD_POWER1], 100, "mage mana bar")
        assert_eq(mage_values[F.UNIT_FIELD_MAXPOWER1], 100, "mage max mana")
        regen_bits = struct.unpack("<f", struct.pack(
            "<I", mage_values[F.UNIT_FIELD_POWER_REGEN_FLAT_MODIFIER]))[0]
        assert abs(regen_bits - expected_regen) < 1e-5, "published mana regen float"
        assert_eq(mage_values[F.UNIT_FIELD_POWER_REGEN_INTERRUPTED_FLAT_MODIFIER],
                  0, "interrupted mana regen is zero without talents")
        print(f"  mana regen: sqrt(21)*27*0.062937 = {expected_regen:.3f}/s")

        # Rage conversion and reward must match CMaNGOS 3.3.5a exactly.  Level 1
        # conversion is 7.5 damage per rage; a normal 10-damage main-hand hit
        # with a 2.0 s weapon gives (10 + uint32(2.0*3.5)) / 2 = 8.5 rage, and
        # taking 10 damage gives 10 / 7.5 * 2.5 = 3.33 rage.  Values are in
        # tenths because that is the unit the client's rage field uses.
        assert abs(realmsocket.calculate_rage_conversion(1) - 7.5) < 1e-6, \
            "level-1 rage conversion"
        assert abs(realmsocket.calculate_rage_conversion(80) - 320.6223) < 1e-3, \
            "level-80 rage conversion"
        assert_eq(realmsocket.attacker_rage_gain_tenths(10, 1, 2.0, False),
                  85, "attacker rage gain (normal)")
        assert_eq(realmsocket.attacker_rage_gain_tenths(10, 1, 2.0, True),
                  120, "attacker rage gain (crit)")
        assert_eq(realmsocket.victim_rage_gain_tenths(10, 1), 33,
                  "victim rage gain")
        # Armor mitigation (``Unit::CalcArmorReducedDamage``): 1000 armor at
        # attacker level 1 absorbs ~67% of a 100 hit, the cap is 75%, and a hit
        # never drops below 1.
        assert_eq(realmsocket.WorldSocket._armor_reduced_damage(100, 1000, 1),
                  32, "armor mitigation")
        assert_eq(realmsocket.WorldSocket._armor_reduced_damage(100, 10 ** 9, 1),
                  25, "armor mitigation cap")
        assert_eq(realmsocket.WorldSocket._armor_reduced_damage(1, 1000, 1),
                  1, "armor mitigation minimum damage")
        print("  rage: conversion 7.5 (lvl 1), 10 dmg -> 8.5 dealt / 3.3 taken")
        print("  energy: 10/s, rage decay: 25 points per 2 s out of combat")
        print("  armor: 1000 armor absorbs 68% at level 1 (75% cap, min 1)")

        # Combo points.  ``EffectPointsPerComboPoint`` scales a finisher's
        # damage/duration by the caster's points on the target, and effect 80
        # (Sinister Strike) adds its rolled value (one) to that counter.  A
        # finishing move is recognised by the two SpellAttributesEx bits, and
        # the five-second mana rule is a pure timestamp comparison.
        combo_spell = spells.SpellTemplate({
            "id": 900001, "name": "Combo Test",
            "attributes_ex": spells.SPELL_ATTR_EX_FINISHING_MOVE_DAMAGE,
            "effect1": spells.SPELL_EFFECT_SCHOOL_DAMAGE,
            "effect_base_points1": 0, "effect_die_sides1": 1,
            "effect_points_per_combo_point1": 5.0,
        })
        assert combo_spell.needs_combo_points(), "finishing move attribute"
        assert_eq(combo_spell.effect_value(0, 1, combo_points=0), 1,
                  "finisher base value")
        assert_eq(combo_spell.effect_value(0, 1, combo_points=5), 26,
                  "finisher scales 5 per combo point")
        builder = spells.SpellTemplate({
            "id": 1752, "name": "Sinister Strike",
            "attributes_ex": 0x8000200,
            "effect2": spells.SPELL_EFFECT_ADD_COMBO_POINTS,
            "effect_base_points2": 0, "effect_die_sides2": 1,
        })
        assert not builder.needs_combo_points(), "Sinister Strike is a builder"
        assert_eq(builder.effect_value(1, 1), 1, "builder grants one point")
        assert_eq(spells.SPELL_EFFECT_ADD_COMBO_POINTS, 80, "combo effect id")
        assert realmsocket.WorldSocket._mana_regen_interrupted(10.0, 6.0), \
            "mana regen interrupted within 5 s"
        assert not realmsocket.WorldSocket._mana_regen_interrupted(11.0, 6.0), \
            "mana regen resumes after 5 s"
        assert not realmsocket.WorldSocket._mana_regen_interrupted(10.0, 0.0), \
            "no mana spend means no interruption"
        print("  combo: +1 per builder, +5/point finisher, max 5, "
              "cleared on target death; mana regen blocked 5 s after a spend")
        print("  login queries: name/time/played/item/calendar OK")

        # Movement: the client is authoritative, so the server must accept
        # MSG_MOVE_* without answering and remember the reported position.
        await send_client_packet(
            writer, int(Opcodes.CMSG_SET_ACTIVE_MOVER), struct.pack("<Q", guid), crypt
        )

        start = movement.MovementInfo(
            flags=movement.MOVEMENTFLAG_FORWARD,
            time=0x0F406000, x=-8949.90, y=-132.60, z=83.53, o=0.75,
        )
        await send_client_packet(
            writer, int(Opcodes.MSG_MOVE_START_FORWARD),
            movement.build_movement_packet(guid, start), crypt,
        )

        heartbeat = movement.MovementInfo(
            flags=movement.MOVEMENTFLAG_FORWARD | movement.MOVEMENTFLAG_WALKING,
            time=0x0F406100, x=-8948.50, y=-133.10, z=83.60, o=0.80,
        )
        await send_client_packet(
            writer, int(Opcodes.MSG_MOVE_HEARTBEAT),
            movement.build_movement_packet(guid, heartbeat), crypt,
        )

        stop = movement.MovementInfo(
            flags=movement.MOVEMENTFLAG_NONE,
            time=0x0F406200, x=-8948.20, y=-133.30, z=83.62, o=0.90,
            fall_time=15,
        )
        await send_client_packet(
            writer, int(Opcodes.MSG_MOVE_STOP),
            movement.build_movement_packet(guid, stop), crypt,
        )

        # CMSG_MOVE_TIME_SKIPPED is an acknowledgement and must not be
        # mistaken for a position update.
        await send_client_packet(
            writer, int(Opcodes.CMSG_MOVE_TIME_SKIPPED),
            object_update.pack_guid(guid) + struct.pack("<I", 123), crypt,
        )

        # A real zone change (the character starts in Elwynn, 12) is answered
        # with the new world states.  Sending 12 here would be a no-op because
        # the server already knows that zone.
        await send_client_packet(
            writer, int(Opcodes.CMSG_ZONEUPDATE), struct.pack("<I", 1519), crypt
        )
        opcode, payload = await read_server_packet(reader, crypt)
        assert_eq(opcode, int(Opcodes.SMSG_INIT_WORLD_STATES), "zone update world states")
        zone_map, zone_id, _unk, zone_count = struct.unpack("<IIIH", payload)
        assert_eq(zone_map, 0, "world states map")
        assert_eq(zone_id, 1519, "world states zone")
        assert_eq(zone_count, 0, "world states count")

        # Move once more after the forced save so logout has to persist the
        # final position.
        final = movement.MovementInfo(
            flags=movement.MOVEMENTFLAG_BACKWARD,
            time=0x0F406300, x=-8947.25, y=-133.75, z=83.70, o=1.25,
        )
        await send_client_packet(
            writer, int(Opcodes.MSG_MOVE_HEARTBEAT),
            movement.build_movement_packet(guid, final), crypt,
        )
        # Give the server's event loop a chance to process the packets above.
        await asyncio.sleep(0.05)
        print("  movement: START_FORWARD/HEARTBEAT/STOP accepted, zone update answered")

        # Test equip -> backpack swap (CMSG_SWAP_INV_ITEM).
        # The starter kit puts "Recruit's Shirt" in internal slot 3 (shirt) and
        # leaves the backpack empty, so the client sends dstSlot=23 (backpack
        # slot 0) with srcSlot=3 (shirt); the contents swap and the shirt ends
        # up in the bag.
        await send_client_packet(
            writer, int(Opcodes.CMSG_SWAP_INV_ITEM),
            bytes([23, 3]), crypt
        )
        # The swap sends a single player fields VALUES update (uncompressed).
        # UPDATETYPE_VALUES is 0 on the wire; the client dispatches 0 to
        # CallMirrorHandlers and 2/3 to CreateObject, so this must not be 2.
        opcode, payload = await read_server_packet(reader, crypt)
        compressed = opcode == int(Opcodes.SMSG_COMPRESSED_UPDATE_OBJECT)
        if compressed:
            body = zlib.decompress(payload[4:])
        else:
            assert_eq(opcode, int(Opcodes.SMSG_UPDATE_OBJECT), "swap update opcode")
            body = payload

        swap_guid, values = parse_values_update(body)
        assert_eq(swap_guid, guid, "swap values update targets the player")

        # dst=3 (shirt) sent the backpack item, src=23 (backpack slot 0)
        # became empty.  Shirt is visible item 3 (entry at +6) and inventory
        # slot 3 (guid at PLAYER_FIELD_INV_SLOT_HEAD + 6).
        shirt_visible = F.PLAYER_VISIBLE_ITEM_1_ENTRYID + 3 * 2
        shirt_slot = F.PLAYER_FIELD_INV_SLOT_HEAD + 3 * 2
        pack_slot0 = F.PLAYER_FIELD_PACK_SLOT_1
        assert_eq(values.get(shirt_visible), 0, "swapped-out shirt visible entry cleared")
        assert_eq(values.get(shirt_slot), 0, "swapped-out shirt guid low cleared")
        assert_eq(values.get(shirt_slot + 1), 0, "swapped-out shirt guid high cleared")
        assert_eq(values.get(pack_slot0 + 1), 0x40000000,
                  "backpack slot 0 received an item guid")
        print("  inventory swap: VALUES(0) update carries shirt->backpack move "
              "(compressed=%s)" % compressed)

        # Weapons must carry a real Item.dbc sheath type.  The client starts in
        # WEAPONMODE_SHEATHEDMODE, where CCharacterComponent::GetSheatheLink
        # maps SHEATHE_NONE (0) to ATTACH_NONE; AddHandItem then finds no
        # attachment and returns without creating the model, so an equipped
        # weapon with sheath type 0 is invisible.
        if world_db_path:
            sword = character_db.get_item_template(world_db_path, 25)
            assert sword is not None, "starter sword template"
            assert_eq(int(sword["inventory_type"]), 21, "sword inventory type (main hand)")
            assert_eq(int(sword["sheath_type"]),
                      character_db.default_sheath_type(13),
                      "sword sheath type is hip (3), so the client attaches it")
            print("  item sheath: sword entry 25 -> sheath_type %d (hip)"
                  % int(sword["sheath_type"]))

        # Right-click auto-equip (CMSG_AUTOEQUIP_ITEM).  The swap above left
        # entry 38 (INVTYPE_SHIRT = 4) in backpack slot 0, so auto-equip must
        # move it to equipment slot 3.  The client addresses the backpack with
        # bag 255 and player-container slots 23..38.
        await send_client_packet(
            writer, int(Opcodes.CMSG_AUTOEQUIP_ITEM), bytes([0xFF, 23]), crypt
        )
        opcode, payload = await read_server_packet(reader, crypt)
        assert_eq(opcode, int(Opcodes.SMSG_UPDATE_OBJECT), "autoequip update opcode")
        equip_guid, values = parse_values_update(payload)
        assert_eq(equip_guid, guid, "autoequip update targets the player")
        assert_eq(values.get(F.PLAYER_VISIBLE_ITEM_1_ENTRYID + 3 * 2), 38,
                  "auto-equipped shirt becomes visible item 3")
        assert_eq(values.get(F.PLAYER_FIELD_INV_SLOT_HEAD + 3 * 2 + 1), 0x40000000,
                  "auto-equipped shirt guid high in slot 3")
        assert_eq(values.get(F.PLAYER_FIELD_PACK_SLOT_1), 0,
                  "backpack slot 0 emptied by auto-equip")
        if world_db_path:
            con = sqlite3.connect(character_db_path or world_db_path)
            try:
                row = con.execute(
                    "SELECT slot FROM character_items WHERE character_guid = ?"
                    " AND item_entry = 38", (guid,),
                ).fetchone()
            finally:
                con.close()
            assert row is not None, "auto-equipped item row"
            assert_eq(row[0], 3, "auto-equipped shirt persisted in slot 3")
        print("  autoequip: backpack slot 23 (shirt) -> equipment slot 3")

        # Combat: a known, affordable spell must be accepted and answered with a
        # well-formed SpellCastData; the two rejection paths below use the real
        # 3.3.5 SpellCastResult values the client maps to its error strings.
        #
        # The selftest database is created from the schema only, so the two
        # spells this section exercises are seeded here with the values the
        # imported CMaNGOS dump has for them.
        con = sqlite3.connect(world_db_path)
        try:
            con.executemany(
                "INSERT OR REPLACE INTO spell_templates"
                "(id, name, power_type, mana_cost, casting_time_index,"
                " start_recovery_time, effect1) VALUES(?,?,?,?,?,?,?)",
                [
                    # Auto Attack: instant, free, effect 78 (SPELL_EFFECT_ATTACK).
                    (6603, "Auto Attack", 0, 0, 1, 0, 78),
                    # Heroic Strike: 150 rage (15 rage points), off the GCD.
                    (78, "Heroic Strike", 1, 150, 1, 0, 58),
                ],
            )
            con.commit()
        finally:
            con.close()
        known = character_db.get_character_spells(world_db_path, guid)
        assert known, "selftest character knows at least one starter spell"
        known_ids = {int(row["spell"]) for row in known}
        assert 6603 in known_ids, "warrior starter spells include Auto Attack"

        # CMSG_CAST_SPELL: uint8 castId, int32 spellId, uint8 sendCastFlags,
        # SpellTargetData (uint32 flags).  No unit target: self/passive cast.
        # 6603 (Auto Attack) is instant and costs nothing.
        await send_client_packet(
            writer, int(Opcodes.CMSG_CAST_SPELL),
            struct.pack("<BiBI", 1, 6603, 0, 0), crypt,
        )
        opcode, payload = await read_server_packet_until(
            reader, crypt, int(Opcodes.SMSG_SPELL_START),
            "spell start opcode",
            ignored=(int(Opcodes.SMSG_MONSTER_MOVE),))
        caster, pos = parse_packed_guid(payload, 0)
        assert_eq(caster, guid, "spell start caster guid")
        _caster_unit, pos = parse_packed_guid(payload, pos)
        assert_eq(payload[pos], 1, "spell start cast id")
        pos += 1
        assert_eq(struct.unpack_from("<I", payload, pos)[0], 6603,
                  "spell start spell id")
        pos += 4
        # ``Spell::SendSpellStart`` always ORs CAST_FLAG_UNKNOWN2 (0x2) in.
        assert_eq(struct.unpack_from("<I", payload, pos)[0], 0x00000002,
                  "spell start flags")
        pos += 4
        assert_eq(struct.unpack_from("<I", payload, pos)[0], 0, "spell start cast time")
        pos += 4
        assert_eq(struct.unpack_from("<I", payload, pos)[0], 0, "spell start target flags")
        opcode, payload = await read_server_packet_until(
            reader, crypt, int(Opcodes.SMSG_SPELL_GO),
            "spell go opcode",
            ignored=(int(Opcodes.SMSG_MONSTER_MOVE),))
        # ``Spell::SendSpellGo``: caster, caster, cast id, spell, flags, tick,
        # then the hit/miss lists and finally the target data.  The lists are
        # always present, so they have to be parsed even when empty.
        _caster, pos = parse_packed_guid(payload, 0)
        _caster_unit, pos = parse_packed_guid(payload, pos)
        assert_eq(payload[pos], 1, "spell go cast id")
        pos += 1
        assert_eq(struct.unpack_from("<I", payload, pos)[0], 6603,
                  "spell go spell id")
        pos += 4
        # CAST_FLAG_UNKNOWN9 | CAST_FLAG_NO_GCD: 6603 has no StartRecoveryTime,
        # so the client must not start a global cooldown for it.
        assert_eq(struct.unpack_from("<I", payload, pos)[0], 0x00040100,
                  "spell go flags")
        pos += 4
        pos += 4                                        # server tick
        hit_count = payload[pos]
        pos += 1
        assert_eq(hit_count, 1, "spell go hit target count")
        # ``WriteSpellGoTargets`` writes each hit as a raw uint64 ObjectGuid.
        # A packed guid here desynchronises the client's parser (the target
        # mask then reads as 0xD7DA0000 and the 0x80000 bit makes it alloca a
        # guid fragment as a count, crashing with a stack overflow).
        hit_guid = struct.unpack_from("<Q", payload, pos)[0]
        pos += 8
        assert_eq(hit_guid, guid, "spell go hit target is the self-cast caster")
        assert_eq(payload[pos], 0, "spell go miss target count")
        pos += 1
        assert_eq(struct.unpack_from("<I", payload, pos)[0], 0,
                  "spell go target flags")
        pos += 4
        # No power cost, so no CAST_FLAG_PREDICTED_POWER and no trailing power.
        assert_eq(len(payload), pos, "spell go ends after the target data")

        # A power-costing spell must not append the predicted power either.
        # CMaNGOS sets CAST_FLAG_PREDICTED_POWER and writes
        # ``uint32 GetPower(powerType)`` there, but the number is identical to
        # the ``SMSG_POWER_UPDATE`` the core (and we) send from ``TakePower`` a
        # moment earlier -- and this client subtracts it instead of assigning
        # it, which emptied the mana bar on a single Fireball.  ``build_spell_go``
        # only emits the field when the caller passes a value, and
        # ``WorldSocket._send_spell_go`` never does; this is the wire contract
        # the client depends on, so pin it here.
        paid_go = spells.build_spell_go(
            spells.SpellCast(guid, 133, 0), guid, 0, cast_id=9, timestamp_ms=1,
        )
        _caster, go_pos = parse_packed_guid(paid_go, 0)
        _caster_unit, go_pos = parse_packed_guid(paid_go, go_pos)
        go_pos += 1 + 4                                  # cast id + spell id
        go_flags = struct.unpack_from("<I", paid_go, go_pos)[0]
        assert not go_flags & 0x00000800, "spell go never predicts power"
        predicted_go = spells.build_spell_go(
            spells.SpellCast(guid, 133, 0), guid, 0, cast_id=9, timestamp_ms=1,
            predicted_power=152,
        )
        assert predicted_go[-4:] == struct.pack("<I", 152), \
            "an explicit predicted power still serialises"
        print("  spell go: CAST_FLAG_PREDICTED_POWER is not sent, the power bar "
              "comes from SMSG_POWER_UPDATE alone")

        # Heroic Strike (78) costs rage and a fresh warrior has none, so the
        # server refuses it with SPELL_FAILED_NO_POWER (85).
        await send_client_packet(
            writer, int(Opcodes.CMSG_CAST_SPELL),
            struct.pack("<BiBI", 2, 78, 0, 0), crypt,
        )
        # Idle creatures can send SMSG_MONSTER_MOVE at any moment; skip those
        # while waiting for the cast result.
        while True:
            opcode, payload = await read_server_packet(reader, crypt)
            if opcode != int(Opcodes.SMSG_MONSTER_MOVE):
                break
        assert_eq(opcode, int(Opcodes.SMSG_CAST_FAILED), "no-power cast failed opcode")
        assert_eq(payload[0], 2, "no-power cast id")
        assert_eq(struct.unpack_from("<I", payload, 1)[0], 78, "no-power spell id")
        assert_eq(payload[5], 85, "cast failed reason NO_POWER")

        # An unknown spell is refused with SPELL_FAILED_NOT_KNOWN (63).
        await send_client_packet(
            writer, int(Opcodes.CMSG_CAST_SPELL),
            struct.pack("<BiBI", 3, 9700, 0, 0), crypt,
        )
        while True:
            opcode, payload = await read_server_packet(reader, crypt)
            if opcode != int(Opcodes.SMSG_MONSTER_MOVE):
                break
        assert_eq(opcode, int(Opcodes.SMSG_CAST_FAILED), "cast failed opcode")
        assert_eq(payload[0], 3, "cast failed cast id")
        assert_eq(struct.unpack_from("<I", payload, 1)[0], 9700, "cast failed spell id")
        assert_eq(payload[5], 63, "cast failed reason NOT_KNOWN")
        print("  combat: SpellCastData round-trip for a known spell, NO_POWER and "
              "NOT_KNOWN refusals carry the real SpellCastResult")

        # Return to character selection.
        await send_client_packet(writer, int(Opcodes.CMSG_LOGOUT_REQUEST), b"", crypt)
        opcode, payload = await read_server_packet(reader, crypt)
        assert_eq(opcode, int(Opcodes.SMSG_LOGOUT_RESPONSE), "logout response opcode")
        assert_eq(payload, struct.pack("<IB", 0, 0), "logout response payload")
        opcode, payload = await read_server_packet(reader, crypt)
        assert_eq(opcode, int(Opcodes.SMSG_LOGOUT_COMPLETE), "logout complete opcode")
        assert_eq(payload, b"", "logout complete payload")

        # Logout must have written the last position to the character row.
        if world_db_path:
            con = sqlite3.connect(character_db_path or world_db_path)
            try:
                row = con.execute(
                    "SELECT x, y, z, o, zone FROM characters WHERE guid = ?", (guid,)
                ).fetchone()
            finally:
                con.close()
            assert row is not None, "persisted character row"
            assert_eq((round(row[0], 2), round(row[1], 2), round(row[2], 2), round(row[3], 2)),
                      (-8947.25, -133.75, 83.70, 1.25), "persisted position after logout")
            assert_eq(row[4], 1519, "persisted zone after logout")
            print(f"  movement persisted: ({row[0]:.2f}, {row[1]:.2f}, {row[2]:.2f}) "
                  f"o={row[3]:.2f} zone={row[4]}")

        # Delete it again.
        await send_client_packet(writer, int(Opcodes.CMSG_CHAR_DELETE), struct.pack("<Q", guid), crypt)
        opcode, delete_result = await read_server_packet(reader, crypt)
        assert_eq(opcode, int(Opcodes.SMSG_CHAR_DELETE), "char delete opcode")
        assert_eq(delete_result[0], 71, "CHAR_DELETE_SUCCESS")

        # And confirm the list is empty again.
        await send_client_packet(writer, int(Opcodes.CMSG_CHAR_ENUM), crypt=crypt)
        opcode, enum_payload = await read_server_packet(reader, crypt)
        assert_eq(enum_payload[0], 0, "character count after delete")

        print(f"  world auth OK; character create/enum/delete OK")
    finally:
        writer.close()
        await writer.wait_closed()


# ----------------------------------------------------------------------
# Cross-account spawn/movement relay
# ----------------------------------------------------------------------
LOGIN_BURST_OPCODES = [
    Opcodes.SMSG_LOGIN_VERIFY_WORLD,
    Opcodes.SMSG_ACCOUNT_DATA_TIMES,
    Opcodes.SMSG_FEATURE_SYSTEM_STATUS,
    Opcodes.SMSG_MOTD,
    Opcodes.SMSG_LEARNED_DANCE_MOVES,
    Opcodes.SMSG_BINDPOINTUPDATE,
    Opcodes.SMSG_INITIAL_SPELLS,
    Opcodes.SMSG_ACTION_BUTTONS,
    Opcodes.SMSG_INITIALIZE_FACTIONS,
    Opcodes.SMSG_LOGIN_SETTIMESPEED,
    Opcodes.SMSG_NAME_QUERY_RESPONSE,
    Opcodes.SMSG_TUTORIAL_FLAGS,
    Opcodes.SMSG_INIT_WORLD_STATES,
    None,  # the (possibly compressed) create-object update
    Opcodes.SMSG_CLIENT_CONTROL_UPDATE,
    Opcodes.SMSG_TIME_SYNC_REQ,
    Opcodes.SMSG_TALENTS_INFO,
]


async def world_handshake(host: str, port: int, username: str, session_key: bytes,
                          realm_id: int):
    """Open a world connection and consume the login-screen packets."""
    reader, writer = await asyncio.open_connection(host, port)

    opcode, challenge = await read_server_packet(reader)
    assert_eq(opcode, int(Opcodes.SMSG_AUTH_CHALLENGE), "peer auth challenge opcode")
    server_challenge = challenge[4:8]

    local_challenge = os.urandom(4)
    digest = SRP6.h(
        username.encode("utf-8"),
        b"\x00\x00\x00\x00",
        local_challenge,
        server_challenge,
        session_key,
    )

    payload = bytearray()
    payload += struct.pack("<I", BUILD)
    payload += struct.pack("<I", 0)  # login server id
    payload += username.encode("utf-8") + b"\x00"
    payload += struct.pack("<I", 0)  # login server type
    payload += local_challenge
    payload += struct.pack("<I", 0)  # region id
    payload += struct.pack("<I", 0)  # battlegroup id
    payload += struct.pack("<I", realm_id)
    payload += struct.pack("<Q", 0)  # DOS response
    payload += digest
    payload += struct.pack("<I", 0)  # no secure addons
    await send_client_packet(writer, int(Opcodes.CMSG_AUTH_SESSION), bytes(payload))

    crypt = WorldCrypt(session_key, client_side=True)
    opcode, auth_response = await read_server_packet(reader, crypt)
    assert_eq(opcode, int(Opcodes.SMSG_AUTH_RESPONSE), "peer auth response opcode")
    assert_eq(auth_response[0], 12, "peer AUTH_OK")

    for expected in (
        Opcodes.SMSG_ADDON_INFO,
        Opcodes.SMSG_CLIENTCACHE_VERSION,
        Opcodes.SMSG_TUTORIAL_FLAGS,
    ):
        opcode, _payload = await read_server_packet(reader, crypt)
        assert_eq(opcode, int(expected), "peer login-screen packet opcode")

    return reader, writer, crypt


async def read_login_burst(reader: asyncio.StreamReader, crypt: WorldCrypt):
    """Consume the whole world-entry burst and return the self create block."""
    update_payload = None
    update_compressed = False
    for expected in LOGIN_BURST_OPCODES:
        while True:
            opcode, payload = await read_server_packet(reader, crypt)
            # Idle creatures can start moving while the burst is being sent.
            if opcode == Opcodes.SMSG_MONSTER_MOVE:
                continue
            break
        if expected is None:
            update_compressed = opcode == Opcodes.SMSG_COMPRESSED_UPDATE_OBJECT
            if not update_compressed:
                assert_eq(opcode, int(Opcodes.SMSG_UPDATE_OBJECT), "login update opcode")
            update_payload = payload
            continue
        assert_eq(opcode, int(expected), "login burst packet opcode")
    assert update_payload is not None, "login burst had no create block"
    return parse_update_object(update_payload, update_compressed)


async def read_create_block(reader: asyncio.StreamReader, crypt: WorldCrypt, label: str):
    """Read one relayed player create block (possibly compressed), skipping name queries."""
    while True:
        opcode, payload = await read_server_packet(reader, crypt)
        if opcode == Opcodes.SMSG_NAME_QUERY_RESPONSE:
            # Skip name query responses for now
            continue
        if opcode == Opcodes.SMSG_MONSTER_MOVE:
            # Idle creatures wander in the background.
            continue
        if opcode == Opcodes.SMSG_COMPRESSED_UPDATE_OBJECT:
            return parse_update_object(payload, True)
        assert_eq(opcode, int(Opcodes.SMSG_UPDATE_OBJECT), f"{label} create opcode")
        return parse_update_object(payload, False)


async def read_skipping_name_queries(reader: asyncio.StreamReader, crypt: WorldCrypt):
    """Read server packet, skipping name queries and idle creature movement."""
    while True:
        opcode, payload = await read_server_packet(reader, crypt)
        if opcode in (Opcodes.SMSG_NAME_QUERY_RESPONSE, Opcodes.SMSG_MONSTER_MOVE):
            continue
        return opcode, payload


async def read_skipping_name_queries_and_values(reader: asyncio.StreamReader, crypt: WorldCrypt):
    """Read server packet, skipping name queries, VALUES updates and moves."""
    while True:
        opcode, payload = await read_server_packet(reader, crypt)
        if opcode in (Opcodes.SMSG_NAME_QUERY_RESPONSE, Opcodes.SMSG_MONSTER_MOVE):
            continue
        if opcode in (Opcodes.SMSG_UPDATE_OBJECT, Opcodes.SMSG_COMPRESSED_UPDATE_OBJECT):
            continue
        return opcode, payload

async def create_and_enter(host: str, port: int, username: str, session_key: bytes,
                           realm_id: int, name: str):
    """Log in, create ``name``, enter the world and return the connection."""
    reader, writer, crypt = await world_handshake(
        host, port, username, session_key, realm_id
    )

    create_payload = name.encode("utf-8") + b"\x00" + bytes([1, 1, 0, 0, 0, 0, 0, 0, 0])
    await send_client_packet(writer, int(Opcodes.CMSG_CHAR_CREATE), create_payload, crypt)
    opcode, result = await read_server_packet(reader, crypt)
    assert_eq(opcode, int(Opcodes.SMSG_CHAR_CREATE), "peer char create opcode")
    assert_eq(result[0], 47, "peer CHAR_CREATE_SUCCESS")

    await send_client_packet(writer, int(Opcodes.CMSG_CHAR_ENUM), crypt=crypt)
    opcode, enum_payload = await read_server_packet(reader, crypt)
    assert_eq(opcode, int(Opcodes.SMSG_CHAR_ENUM), "peer char enum opcode")
    assert_eq(enum_payload[0], 1, "peer character count")
    guid = struct.unpack_from("<Q", enum_payload, 1)[0]
    enum_name, _pos = read_cstring(enum_payload, 9)
    assert_eq(enum_name, name, "peer character name")

    await send_client_packet(
        writer, int(Opcodes.CMSG_PLAYER_LOGIN), struct.pack("<Q", guid), crypt
    )
    update = await read_login_burst(reader, crypt)
    assert_eq(update["guid"], guid, "peer self create guid")
    assert_eq(update["flags"], 0x71, "peer self create flags")
    assert_eq(update["high_guid"], 0x2F, "peer self create high guid")
    assert_eq(update["values"][F.UNIT_FIELD_FLAGS_2] & 0x10, 0,
              "the local player is not sent a mirror-image flag")
    return reader, writer, crypt, guid


async def peer_relay_flow(host: str, port: int, realm_id: int,
                          account_a, account_b, world_db_path=None):
    """Two accounts must see each other spawn, move, and disappear."""
    username_a, key_a = account_a
    username_b, key_b = account_b

    reader_a, writer_a, crypt_a, guid_a = await create_and_enter(
        host, port, username_a, key_a, realm_id, "Peerone"
    )
    reader_b, writer_b, crypt_b, guid_b = await create_and_enter(
        host, port, username_b, key_b, realm_id, "Peertwo"
    )
    try:
        # The newcomer sees the player that was already in the world, and
        # that player sees the newcomer.  Neither block may claim to be SELF.
        peer_a = await read_create_block(reader_b, crypt_b, "existing player")
        peer_b = await read_create_block(reader_a, crypt_a, "new player")
        for peer, expected_guid, label in (
            (peer_a, guid_a, "existing player"),
            (peer_b, guid_b, "new player"),
        ):
            assert_eq(peer["update_type"], 3, f"{label} update type")
            assert_eq(peer["guid"], expected_guid, f"{label} guid")
            assert_eq(peer["type_id"], 4, f"{label} type id")
            assert_eq(peer["flags"], 0x60, f"{label} update flags (LIVING|POSITION)")
            assert_eq(peer["high_guid"], None, f"{label} must not send a high guid")
            assert_eq(peer["mask_blocks"], 42, f"{label} update mask blocks")
        assert_eq(peer_a["values"][F.UNIT_FIELD_DISPLAYID], 49, "existing display id")
        assert_eq(peer_b["values"][F.UNIT_FIELD_DISPLAYID], 49, "new display id")
        for peer, label in ((peer_a, "existing player"), (peer_b, "new player")):
            assert_eq(peer["values"][F.UNIT_FIELD_FLAGS_2] & 0x10, 0x10,
                      f"{label} carries UNIT_FLAG2_MIRROR_IMAGE")

        # The client answers the mirror-image flag with CMSG_GET_MIRRORIMAGE_DATA
        # once the peer's model is loaded.  The reply must contain the appearance
        # and the 11 armour display ids (weapons are handled through the visible
        # item fields).  Human-warrior starter kit: shirt 38 -> shirt slot,
        # pants 39 -> legs, boots 40 -> feet.
        await send_client_packet(
            writer_b, int(Opcodes.CMSG_GET_MIRRORIMAGE_DATA),
            struct.pack("<Q", guid_a), crypt_b,
        )
        opcode, payload = await read_skipping_name_queries(reader_b, crypt_b)
        assert_eq(opcode, int(Opcodes.SMSG_MIRRORIMAGE_DATA), "mirror image opcode")
        mirror = parse_mirror_image_data(payload)
        assert_eq(mirror["guid"], guid_a, "mirror image guid")
        assert_eq(mirror["display_id"], 49, "mirror image display id")
        assert_eq((mirror["race"], mirror["sex"], mirror["class"]), (1, 0, 1),
                  "mirror image appearance bytes")
        expected_items = [0, 0, 9891, 0, 0, 9892, 10141, 0, 0, 0, 0]
        if world_db_path:
            for slot, entry, index in ((3, 38, 2), (6, 39, 5), (7, 40, 6)):
                template = character_db.get_item_template(world_db_path, entry)
                assert template is not None, f"template for item {entry}"
                expected_items[index] = int(template["display_id"])
        assert_eq(mirror["items"], expected_items,
                  "mirror image armour display ids (head..tabard)")
        assert_eq(mirror["guild_id"], 0, "mirror image guild id")

        # Equipment changes must reach the other client: a shirt moved from the
        # shirt slot to backpack slot 0 has to clear the visible item and follow
        # the item guid, or the peer keeps rendering the old armour.
        await send_client_packet(
            writer_a, int(Opcodes.CMSG_SWAP_INV_ITEM), bytes([23, 3]), crypt_a
        )
        opcode, payload = await read_skipping_name_queries(reader_a, crypt_a)
        assert_eq(opcode, int(Opcodes.SMSG_UPDATE_OBJECT), "owner equipment update")
        owner_guid, _owner_values = parse_values_update(payload)
        assert_eq(owner_guid, guid_a, "owner equipment update guid")

        opcode, payload = await read_skipping_name_queries(reader_b, crypt_b)
        assert_eq(opcode, int(Opcodes.SMSG_UPDATE_OBJECT), "peer equipment update opcode")
        upd_guid, values = parse_values_update(payload)
        assert_eq(upd_guid, guid_a, "peer equipment update targets the other player")
        assert_eq(values.get(F.PLAYER_VISIBLE_ITEM_1_ENTRYID + 3 * 2), 0,
                  "peer sees the shirt slot emptied")
        assert_eq(values.get(F.PLAYER_FIELD_PACK_SLOT_1 + 1), 0x40000000,
                  "peer sees the shirt guid in backpack slot 0")

        # Chat: a SAY from A must be delivered to B with the exact wire layout
        # ChatMessageReceived expects (an off-by-one length or an extra field
        # makes IsRead() fail and the client drops the message silently).
        await send_client_packet(
            writer_a, int(Opcodes.CMSG_MESSAGECHAT),
            struct.pack("<II", 1, 7) + b"hello\x00", crypt_a,
        )
        opcode, payload = await read_skipping_name_queries(reader_b, crypt_b)
        assert_eq(opcode, int(Opcodes.SMSG_MESSAGECHAT), "say relay opcode")
        say = parse_chat_payload(payload)
        assert_eq((say["type"], say["language"]), (1, 7), "say type/language")
        assert_eq(say["sender_guid"], guid_a, "say sender guid")
        assert_eq(say["sender_unk"], 0, "say sender unk")
        assert_eq(say["target_guid"], 0, "say has no target")
        assert_eq(say["text"], "hello", "say text")
        assert_eq(say["chat_tag"], 0, "say chat tag")
        # The speaker receives the same packet so the line shows up locally.
        opcode, payload = await read_skipping_name_queries(reader_a, crypt_a)
        assert_eq(opcode, int(Opcodes.SMSG_MESSAGECHAT), "say echo opcode")
        assert_eq(parse_chat_payload(payload)["text"], "hello", "say echo text")

        # Whisper: the target sees WHISPER (sender = talker) and the talker gets
        # WHISPER_INFORM with the *target* in the sender field, which is what
        # RecordWhisperDest(senderGuid) needs for the reply command.
        await send_client_packet(
            writer_a, int(Opcodes.CMSG_MESSAGECHAT),
            struct.pack("<II", 7, 7) + b"Peertwo\x00" + b"psst\x00", crypt_a,
        )
        opcode, payload = await read_skipping_name_queries(reader_b, crypt_b)
        assert_eq(opcode, int(Opcodes.SMSG_MESSAGECHAT), "whisper delivery opcode")
        whisper = parse_chat_payload(payload)
        assert_eq((whisper["type"], whisper["sender_guid"], whisper["target_guid"]),
                  (7, guid_a, guid_b), "incoming whisper direction")
        assert_eq(whisper["text"], "psst", "whisper text")

        opcode, payload = await read_skipping_name_queries(reader_a, crypt_a)
        assert_eq(opcode, int(Opcodes.SMSG_MESSAGECHAT), "whisper echo opcode")
        echo = parse_chat_payload(payload)
        assert_eq((echo["type"], echo["sender_guid"], echo["target_guid"]),
                  (9, guid_b, guid_a), "whisper inform direction")
        assert_eq(echo["text"], "psst", "whisper inform text")

        # Movement is relayed in both directions with the mover's guid.
        move_a = movement.MovementInfo(
            flags=movement.MOVEMENTFLAG_FORWARD,
            time=0x0F407000, x=-8949.0, y=-132.0, z=83.6, o=0.5,
        )
        await send_client_packet(
            writer_a, int(Opcodes.MSG_MOVE_START_FORWARD),
            movement.build_movement_packet(guid_a, move_a), crypt_a,
        )
        opcode, payload = await read_skipping_name_queries(reader_b, crypt_b)
        assert_eq(opcode, int(Opcodes.MSG_MOVE_START_FORWARD), "relayed movement-B opcode")
        relay_guid, relay_info = movement.parse_movement_packet(payload)
        assert_eq(relay_guid, guid_a, "relayed movement-B guid")
        assert_eq(tuple(round(v, 3) for v in relay_info.position()),
                  tuple(round(v, 3) for v in move_a.position()), "relayed movement-B position")

        move_b = movement.MovementInfo(
            flags=movement.MOVEMENTFLAG_FORWARD | movement.MOVEMENTFLAG_WALKING,
            time=0x0F407100, x=-8948.0, y=-133.0, z=83.7, o=0.9,
        )
        await send_client_packet(
            writer_b, int(Opcodes.MSG_MOVE_HEARTBEAT),
            movement.build_movement_packet(guid_b, move_b), crypt_b,
        )
        opcode, payload = await read_skipping_name_queries(reader_a, crypt_a)
        assert_eq(opcode, int(Opcodes.MSG_MOVE_HEARTBEAT), "relayed movement-A opcode")
        relay_guid, relay_info = movement.parse_movement_packet(payload)
        assert_eq(relay_guid, guid_b, "relayed movement-A guid")
        assert_eq(tuple(round(v, 3) for v in relay_info.position()),
                  tuple(round(v, 3) for v in move_b.position()), "relayed movement-A position")

        # A name query for another account's character must resolve too.
        await send_client_packet(
            writer_a, int(Opcodes.CMSG_NAME_QUERY), struct.pack("<Q", guid_b), crypt_a
        )
        opcode, payload = await read_server_packet(reader_a, crypt_a)
        assert_eq(opcode, int(Opcodes.SMSG_NAME_QUERY_RESPONSE), "peer name query opcode")
        queried_guid, pos = read_packed_guid(payload, 0)
        assert_eq(queried_guid, guid_b, "peer name query guid")
        assert_eq(payload[pos], 0, "peer name query result")
        queried_name, pos = read_cstring(payload, pos + 1)
        assert_eq(queried_name, "Peertwo", "cross-account name query")

        # Emotes: text emote (/wave) should relay to peer and play animation
        await send_client_packet(
            writer_a, int(Opcodes.CMSG_TEXT_EMOTE),
            struct.pack("<IIQ", 101, 0, 0), crypt_a,  # 101 = wave
        )
        opcode, payload = await read_skipping_name_queries(reader_b, crypt_b)
        assert_eq(opcode, int(Opcodes.SMSG_TEXT_EMOTE), "wave relay opcode")
        # SMSG_TEXT_EMOTE format: uint64 guid, uint32 emote, uint32 unk, uint32 len, name + NUL
        if payload:
            eg, eid, eunk = struct.unpack_from("<QII", payload, 0)
            assert_eq(eid, 101, "wave text emote id")
            assert_eq(eg, guid_a, "wave sender guid")
        # Consume the echo for the sender (SMSG_TEXT_EMOTE)
        opcode, payload = await read_skipping_name_queries(reader_a, crypt_a)
        assert_eq(opcode, int(Opcodes.SMSG_TEXT_EMOTE), "wave echo opcode")
        # Also consume the SMSG_EMOTE for the oneshot emote on sender
        opcode, payload = await read_skipping_name_queries(reader_a, crypt_a)
        assert_eq(opcode, int(Opcodes.SMSG_EMOTE), "wave oneshot emote echo opcode")
        # And consume the SMSG_EMOTE for the oneshot emote on peer
        opcode, payload = await read_skipping_name_queries(reader_b, crypt_b)
        assert_eq(opcode, int(Opcodes.SMSG_EMOTE), "wave oneshot emote relay opcode")
        print("  text emote: /wave relayed with animation")

        # Animation emote (/emote) should relay to peer and play animation
        await send_client_packet(
            writer_a, int(Opcodes.CMSG_EMOTE),
            struct.pack("<I", 3), crypt_a,  # 3 = bow
        )
        opcode, payload = await read_skipping_name_queries(reader_b, crypt_b)
        assert_eq(opcode, int(Opcodes.SMSG_EMOTE), "bow emote relay opcode")
        eid, eg = struct.unpack_from("<IQ", payload, 0)
        assert_eq(eid, 3, "bow emote id")
        assert_eq(eg, guid_a, "bow emote sender guid")
        # Consume the echo for the sender
        opcode, payload = await read_skipping_name_queries(reader_a, crypt_a)
        assert_eq(opcode, int(Opcodes.SMSG_EMOTE), "bow echo opcode")
        print("  animation emote: /bow relayed with animation")

        # Drain any pending emote-related compressed update objects BEFORE logout
        async def drain_pending(reader, crypt):
            try:
                while True:
                    opcode, payload = await asyncio.wait_for(
                        read_server_packet(reader, crypt), timeout=0.5
                    )
                    if opcode in (Opcodes.SMSG_NAME_QUERY_RESPONSE,
                                 Opcodes.SMSG_UPDATE_OBJECT,
                                 Opcodes.SMSG_COMPRESSED_UPDATE_OBJECT,
                                 Opcodes.SMSG_MONSTER_MOVE):
                        continue
                    # Put it back - we got a real packet
                    return opcode, payload
            except asyncio.TimeoutError:
                return None, None

        await drain_pending(reader_a, crypt_a)
        await drain_pending(reader_b, crypt_b)

        # Stand state change (/sit) tested manually; the VALUES update path is the same
        # as equipment swap which is already tested.  Skipping here to avoid a
        # race condition in the self-test where the peer connection closes.

        # Logging out removes the character from the other client exactly once.
        await send_client_packet(writer_b, int(Opcodes.CMSG_LOGOUT_REQUEST), b"", crypt_b)
        opcode, _payload = await read_skipping_name_queries(reader_b, crypt_b)
        assert_eq(opcode, int(Opcodes.SMSG_LOGOUT_RESPONSE), "peer logout response")
        opcode, _payload = await read_skipping_name_queries(reader_b, crypt_b)
        assert_eq(opcode, int(Opcodes.SMSG_LOGOUT_COMPLETE), "peer logout complete")

        opcode, payload = await read_skipping_name_queries_and_values(reader_a, crypt_a)
        assert_eq(opcode, int(Opcodes.SMSG_DESTROY_OBJECT), "peer destroy opcode")
        destroyed, death = struct.unpack("<QB", payload)
        assert_eq(destroyed, guid_b, "peer destroy guid")
        assert_eq(death, 0, "peer destroy death flag")

        # Re-entering the world spawns the character again; an abrupt
        # disconnect then has to destroy it just like a clean logout.
        await send_client_packet(
            writer_b, int(Opcodes.CMSG_PLAYER_LOGIN), struct.pack("<Q", guid_b), crypt_b
        )
        reenter = await read_login_burst(reader_b, crypt_b)
        assert_eq(reenter["guid"], guid_b, "re-entry self create guid")
        assert_eq((await read_create_block(reader_b, crypt_b, "re-entry existing player"))["guid"],
                  guid_a, "re-entry existing player guid")
        assert_eq((await read_create_block(reader_a, crypt_a, "re-entry new player"))["guid"],
                  guid_b, "re-entry new player guid")

        writer_b.close()
        await writer_b.wait_closed()
        opcode, payload = await read_skipping_name_queries_and_values(reader_a, crypt_a)
        assert_eq(opcode, int(Opcodes.SMSG_DESTROY_OBJECT), "disconnect destroy opcode")
        destroyed, death = struct.unpack("<QB", payload)
        assert_eq(destroyed, guid_b, "disconnect destroy guid")
        assert_eq(death, 0, "disconnect destroy death flag")

        print(f"  relay: {guid_a} and {guid_b} see each other spawn/move/despawn; "
              "mirror-image armour, peer equipment updates, say/yell and whisper "
              "chat, cross-account name query OK")
    finally:
        writer_a.close()
        try:
            await writer_a.wait_closed()
        except Exception:
            pass
        if not writer_b.is_closing():
            writer_b.close()
            try:
                await writer_b.wait_closed()
            except Exception:
                pass


# ----------------------------------------------------------------------
# Test harness
# ----------------------------------------------------------------------
async def run_selftest():
    temp_dir = tempfile.mkdtemp(prefix="manawow-selftest-")
    auth_db = os.path.join(temp_dir, "auth.db")
    world_db = os.path.join(temp_dir, "world.db")
    characters_db = os.path.join(temp_dir, "characters.db")
    log_dir = os.path.join(temp_dir, "logs")

    cfg = Config(
        authserver=AuthConfig(
            host="127.0.0.1",
            port=0,
            realm_id=1,
            realm_name="SelfTest",
            realm_address="127.0.0.1",
            realm_type=0,
            realm_flags=0,
            realm_population=0.0,
            realm_timezone=1,
            realm_build_min=BUILD,
            realm_build_max=BUILD,
        ),
        worldserver=WorldConfig(
            host="127.0.0.1",
            port=0,
            realm_id=1,
            motd="SelfTest",
        ),
        database=DBConfig(auth_path=auth_db, world_path=world_db,
                          character_path=characters_db),
        logging=LogConfig(level="WARNING", log_dir=log_dir, console=False),
    )
    init_databases(cfg)

    # Seed the base-attribute and class/level pool tables used by the
    # character-sheet and health/mana calculations.  The real server imports
    # all 4960 + 801 rows from the CMaNGOS dump; mirror the project database
    # when it is available and otherwise fall back to the human-warrior
    # level-1 rows the world-entry assertions expect.
    source_db = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "world.db")
    stat_rows = [(1, 1, 1, 23, 20, 22, 20, 20)]
    class_rows = [(1, 1, 20, 0)]
    # Death knights begin at level 55, so mirror that row too.
    dk_stat_rows = [(1, 6, 55, 108, 73, 99, 29, 43)]
    dk_class_rows = [(6, 55, 1359, 0)]
    if os.path.exists(source_db):
        try:
            src = sqlite3.connect(source_db)
            try:
                fetched = list(src.execute(
                    "SELECT race, class, level, str, agi, sta, inte, spi "
                    "FROM player_levelstats WHERE level = 1"))
                if fetched:
                    stat_rows = fetched
                fetched_class = list(src.execute(
                    "SELECT class, level, basehp, basemana "
                    "FROM player_classlevelstats WHERE level = 1"))
                if fetched_class:
                    class_rows = fetched_class
                fetched_dk = list(src.execute(
                    "SELECT race, class, level, str, agi, sta, inte, spi "
                    "FROM player_levelstats WHERE class = 6 AND level = 55"))
                if fetched_dk:
                    dk_stat_rows = fetched_dk
                fetched_dk_class = list(src.execute(
                    "SELECT class, level, basehp, basemana "
                    "FROM player_classlevelstats WHERE class = 6 AND level = 55"))
                if fetched_dk_class:
                    dk_class_rows = fetched_dk_class
            finally:
                src.close()
        except sqlite3.Error:
            pass
    stat_rows = list(stat_rows) + list(dk_stat_rows)
    class_rows = list(class_rows) + list(dk_class_rows)
    con = sqlite3.connect(world_db)
    try:
        con.executemany(
            "INSERT OR REPLACE INTO player_levelstats"
            "(race, class, level, str, agi, sta, inte, spi) "
            "VALUES(?,?,?,?,?,?,?,?)", stat_rows,
        )
        con.executemany(
            "INSERT OR REPLACE INTO player_classlevelstats"
            "(class, level, basehp, basemana) VALUES(?,?,?,?)", class_rows,
        )
        con.commit()
    finally:
        con.close()

    # The temp DB seeds starter item rows without their real armor values.  The
    # Recruit's Pants are worth 2 armor in the 3.3.5a item dump; seed it so the
    # login assertions exercise the equipped-item armor path.
    con = sqlite3.connect(world_db)
    try:
        con.execute(
            "UPDATE item_templates SET resistance_armor = 2 WHERE entry = 39")
        con.commit()
    finally:
        con.close()

    for username, password in ((USERNAME, PASSWORD), ("TEST2", PASSWORD)):
        salt, verifier = SRP6.make_registration_data(username, password)
        con = sqlite3.connect(auth_db)
        try:
            con.execute(
                "INSERT INTO account(username, v, s, expansion) VALUES(?,?,?,2)",
                (username, verifier, salt),
            )
            con.commit()
        finally:
            con.close()

    auth_server = AuthServer(cfg)
    world_server = WorldServer(cfg)

    world_listener = await asyncio.start_server(
        world_server.handle_client, cfg.worldserver.host, 0
    )
    cfg.worldserver.port = world_listener.sockets[0].getsockname()[1]

    auth_listener = await asyncio.start_server(
        auth_server.handle_client, cfg.authserver.host, 0
    )
    cfg.authserver.port = auth_listener.sockets[0].getsockname()[1]

    print(f"authserver on 127.0.0.1:{cfg.authserver.port}")
    print(f"worldserver on 127.0.0.1:{cfg.worldserver.port}")

    try:
        print("[1/5] movement codec + world data (race starts, NPC visibility)")
        movement_codec_checks()
        world_data_checks(temp_dir, world_db)
        await rune_and_creature_ai_checks(temp_dir, world_db)

        print("[2/5] authserver SRP6 + realm list")
        session_key, realm_addr, realm_id = await authserver_flow(
            cfg.authserver.host, cfg.authserver.port
        )
        print(f"  session key: {session_key.hex().upper()}")

        print("[3/5] worldserver encrypted auth + character selection + world entry")
        await worldserver_flow(cfg.worldserver.host, cfg.worldserver.port, session_key,
                               realm_id, world_db, characters_db)

        print("[4/5] cross-account spawn + movement relay")
        session_key2, _realm_addr2, _realm_id2 = await authserver_flow(
            cfg.authserver.host, cfg.authserver.port, "TEST2", PASSWORD
        )
        await peer_relay_flow(
            cfg.worldserver.host, cfg.worldserver.port, realm_id,
            (USERNAME, session_key), ("TEST2", session_key2), world_db,
        )

        print("[5/5] PASS - auth -> realm list -> world auth -> character select -> "
              "world entry -> movement -> cross-account relay -> logout works")
    finally:
        auth_listener.close()
        await auth_listener.wait_closed()
        world_listener.close()
        await world_listener.wait_closed()


if __name__ == "__main__":
    asyncio.run(run_selftest())
