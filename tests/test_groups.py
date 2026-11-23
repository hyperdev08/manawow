"""Party/raid membership, group list packets and loot serialization."""
from __future__ import annotations

import asyncio
import os
import struct
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src.protocol.opcodes import Opcodes
from src.world import group_packets
from src.world.groups import GroupManager


class FakeSession:
    def __init__(self, guid, name, map_id=0):
        self.player_guid = guid
        self.character = {
            "guid": guid, "name": name, "map": map_id, "health": 100,
            "max_health": 100, "power": 50, "max_power": 100, "level": 10,
            "zone": 12, "x": 1.5, "y": -2.5,
        }
        self.in_world = True
        self.sent = []
        self.combat = {"dead": False}

    async def send_packet(self, opcode, payload=b""):
        self.sent.append((opcode, payload))

    def _get_combat_state(self):
        return self.combat

    def _power_type(self):
        return 0


class FakeServer:
    def __init__(self, *sessions):
        self.sessions = list(sessions)
        self.groups = GroupManager(self)


def test_invite_accept_group_list_and_leader():
    leader = FakeSession(100, "Leader")
    target = FakeSession(200, "Target")
    server = FakeServer(leader, target)
    manager = server.groups

    asyncio.run(manager.invite(leader, "Target"))
    assert any(op == Opcodes.SMSG_GROUP_INVITE for op, _ in target.sent)
    group = manager.group_for(100)
    assert group is not None and target.player_guid in group.invites

    asyncio.run(manager.accept(target))
    assert manager.same_group(100, 200)
    assert group.size == 2
    assert any(op == Opcodes.SMSG_GROUP_LIST for op, _ in leader.sent)
    assert any(op == Opcodes.SMSG_GROUP_LIST for op, _ in target.sent)
    assert any(op == Opcodes.SMSG_PARTY_MEMBER_STATS_FULL
               for op, _ in target.sent)


def test_leave_transfers_leader_and_loot_locks():
    leader = FakeSession(100, "Leader")
    member = FakeSession(200, "Member")
    third = FakeSession(300, "Third")
    server = FakeServer(leader, member, third)
    manager = server.groups
    asyncio.run(manager.invite(leader, "Member"))
    asyncio.run(manager.accept(member))
    asyncio.run(manager.invite(leader, "Third"))
    asyncio.run(manager.accept(third))

    assert manager.acquire_loot(77, 100)
    assert not manager.acquire_loot(77, 200)
    manager.release_loot(77, 100)
    assert manager.acquire_loot(77, 200)

    asyncio.run(manager.leave(leader))
    group = manager.group_for(200)
    assert group is not None and group.leader_guid == 200
    assert manager.group_for(100) is None


def test_group_list_and_member_stats_layouts():
    leader = FakeSession(100, "Leader")
    member = FakeSession(200, "Member")
    server = FakeServer(leader, member)
    manager = server.groups
    asyncio.run(manager.invite(leader, "Member"))
    asyncio.run(manager.accept(member))
    group = manager.group_for(100)

    payload = group_packets.build_group_list(group, 100)
    pos = 0
    flags, subgroup, member_flags, lfg_roles = struct.unpack_from(
        "<BBBB", payload, pos)
    pos += 4
    group_guid, counter, count = struct.unpack_from("<QII", payload, pos)
    pos += 16
    assert flags == 0 and subgroup == 0 and member_flags == 0
    assert group_guid == group.guid and count == 1
    end = payload.index(b"\x00", pos)
    assert payload[pos:end] == b"Member"
    pos = end + 1
    guid, status, subgroup, member_flags, lfg_roles = struct.unpack_from(
        "<QBBBB", payload, pos)
    pos += 12
    assert guid == 200 and status & 1 and subgroup == 0
    leader_guid, = struct.unpack_from("<Q", payload, pos)
    pos += 8
    assert leader_guid == 100
    loot_method, master, threshold, dungeon, raid, heroic = struct.unpack_from(
        "<BQBBBB", payload, pos)
    assert loot_method == 0 and master == 0
    assert (threshold, dungeon, raid, heroic) == (2, 0, 0, 0)

    stats = group_packets.build_party_member_stats_full(member)
    assert stats[0] == 0
    # packed guid mask byte followed by the full update mask.
    packed_len = 1 + bin(stats[1]).count("1")
    mask, = struct.unpack_from("<I", stats, packed_len + 1)
    assert mask == 0x00040BFF


def test_raid_conversion_and_disband():
    leader = FakeSession(100, "Leader")
    member = FakeSession(200, "Member")
    server = FakeServer(leader, member)
    manager = server.groups
    asyncio.run(manager.invite(leader, "Member"))
    asyncio.run(manager.accept(member))
    group = manager.group_for(100)

    asyncio.run(manager.convert_raid(leader))
    assert group.raid
    asyncio.run(manager.disband(group))
    assert manager.group_for(100) is None
    assert manager.group_for(200) is None
    assert any(op == Opcodes.SMSG_GROUP_DESTROYED for op, _ in leader.sent)
