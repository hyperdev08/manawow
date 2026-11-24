"""Persistent groups, loot rolls, ready checks and raid target icons."""
from __future__ import annotations

import asyncio
import os
import struct
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src.protocol.opcodes import Opcodes
from src.world import character_db, group_packets
from src.world.groups import GroupManager


class FakeSession:
    def __init__(self, guid, name, map_id=0):
        self.player_guid = guid
        self.character = {
            "guid": guid, "name": name, "map": map_id, "health": 100,
            "max_health": 100, "power": 50, "max_power": 100, "level": 10,
            "zone": 12, "x": float(guid), "y": 0.0, "z": 0.0,
        }
        self.in_world = True
        self.sent = []
        self.combat = {"dead": False}
        self.awards = []

    async def send_packet(self, opcode, payload=b""):
        self.sent.append((opcode, payload))

    def _get_combat_state(self):
        return self.combat

    def _power_type(self):
        return 0

    async def _loot_slot_entry(self, loot_target, slot):
        return 200, 1

    async def _award_rolled_loot(self, loot_target, low_guid, item_entry,
                                 count, slot):
        self.awards.append((loot_target, low_guid, item_entry, count, slot))
        return True


class FakeServer:
    def __init__(self, *sessions):
        self.sessions = list(sessions)
        self.groups = GroupManager(self)


def _make_group(*sessions):
    server = FakeServer(*sessions)
    manager = server.groups
    leader, member = sessions[0], sessions[1]
    asyncio.run(manager.invite(leader, member.character["name"]))
    asyncio.run(manager.accept(member))
    return manager, manager.group_for(leader.player_guid)


def _make_db(tmp_path):
    path = str(tmp_path / "groups.db")
    character_db.init_character_db(path)
    leader_guid = character_db.create_character(
        path, 1, "Leader", 1, 1, 0, 0, 0, 0, 0, 0)
    member_guid = character_db.create_character(
        path, 2, "Member", 1, 1, 0, 0, 0, 0, 0, 0)
    return path, leader_guid, member_guid


def test_groups_persist_offline_members_and_settings(tmp_path):
    path, leader_guid, member_guid = _make_db(tmp_path)
    leader = FakeSession(leader_guid, "Leader")
    member = FakeSession(member_guid, "Member")
    server = FakeServer(leader, member)
    manager = server.groups
    manager.load(path)
    asyncio.run(manager.invite(leader, "Member"))
    asyncio.run(manager.accept(member))
    group = manager.group_for(leader_guid)
    assert group is not None

    asyncio.run(manager.set_loot_method(
        leader, group_packets.LOOT_METHOD_MASTER_LOOT, leader_guid, 4))
    asyncio.run(manager.set_assistant(leader, member_guid, True))
    asyncio.run(manager.set_target_icon(leader, 2, 4242))
    creature_marker = 0xF13000000000ABCD
    asyncio.run(manager.set_target_icon(leader, 3, creature_marker))

    # A logout keeps the member and the persisted group.
    member.in_world = False
    asyncio.run(manager.on_session_end(member_guid))
    assert manager.group_for(member_guid) is group
    assert group.is_member(member_guid)

    # Simulate a server restart.
    restarted = GroupManager(FakeServer())
    restarted.load(path)
    loaded = restarted.group_for(leader_guid)
    assert loaded is not None
    assert loaded.is_member(member_guid)
    assert loaded.leader_guid == leader_guid
    assert loaded.loot_method == group_packets.LOOT_METHOD_MASTER_LOOT
    assert loaded.master_looter_guid == leader_guid
    assert loaded.loot_threshold == 4
    assert loaded.target_icons[2] == 4242
    assert loaded.target_icons[3] == creature_marker
    assert loaded.members[member_guid].member_flags \
        & group_packets.MEMBER_FLAG_ASSISTANT

    # Logging back in restores the group list, member stats and icon list.
    restarted.server.sessions = [leader]
    leader.sent.clear()
    asyncio.run(restarted.on_player_login(leader))
    assert any(op == Opcodes.SMSG_GROUP_LIST for op, _ in leader.sent)
    assert any(op == Opcodes.SMSG_PARTY_MEMBER_STATS_FULL
               for op, _ in leader.sent)
    assert any(op == Opcodes.MSG_RAID_TARGET_UPDATE
               for op, _ in leader.sent)

    # The offline member is listed with status OFFLINE (0).
    payload = group_packets.build_group_list(loaded, leader_guid)
    pos = 4
    _guid, _counter, count = struct.unpack_from("<QII", payload, pos)
    pos += 16
    assert count == 1
    end = payload.index(b"\x00", pos)
    assert payload[pos:end] == b"Member"
    pos = end + 1
    guid, status = struct.unpack_from("<QB", payload, pos)
    assert guid == member_guid
    assert status == group_packets.MEMBER_STATUS_OFFLINE


def test_fully_offline_group_expires_after_reconnect_window(tmp_path):
    path, leader_guid, member_guid = _make_db(tmp_path)
    leader = FakeSession(leader_guid, "Leader")
    member = FakeSession(member_guid, "Member")
    server = FakeServer(leader, member)
    manager = server.groups
    manager.load(path)
    asyncio.run(manager.invite(leader, "Member"))
    asyncio.run(manager.accept(member))
    group = manager.group_for(leader_guid)
    assert group is not None

    member.in_world = False
    asyncio.run(manager.on_session_end(member_guid, member))
    assert group.offline_since == 0       # leader is still online
    leader.in_world = False
    asyncio.run(manager.on_session_end(leader_guid, leader))
    assert manager.group_for(leader_guid) is group
    assert group.offline_since > 0

    # Persist the offline timestamp across a restart.
    restarted = GroupManager(FakeServer())
    restarted.load(path)
    loaded = restarted.group_for(leader_guid)
    assert loaded is not None
    assert loaded.offline_since == group.offline_since

    base = int(group.offline_since)
    asyncio.run(restarted.expire_offline_groups(
        base + restarted.offline_expiry - 1))
    assert restarted.group_for(leader_guid) is loaded

    restarted._last_offline_scan = 0
    asyncio.run(restarted.expire_offline_groups(
        base + restarted.offline_expiry))
    assert restarted.group_for(leader_guid) is None

    final = GroupManager(FakeServer())
    final.load(path)
    assert final.group_for(leader_guid) is None


def test_login_inside_window_keeps_group_and_clears_offline_time(tmp_path):
    path, leader_guid, member_guid = _make_db(tmp_path)
    leader = FakeSession(leader_guid, "Leader")
    member = FakeSession(member_guid, "Member")
    server = FakeServer(leader, member)
    manager = server.groups
    manager.load(path)
    asyncio.run(manager.invite(leader, "Member"))
    asyncio.run(manager.accept(member))
    group = manager.group_for(leader_guid)

    member.in_world = False
    asyncio.run(manager.on_session_end(member_guid, member))
    leader.in_world = False
    asyncio.run(manager.on_session_end(leader_guid, leader))
    assert group.offline_since > 0

    leader.in_world = True
    server.sessions = [leader]
    asyncio.run(manager.on_player_login(leader))
    assert manager.group_for(leader_guid) is group
    assert group.offline_since == 0
    assert any(op == Opcodes.SMSG_GROUP_LIST for op, _ in leader.sent)


def test_login_after_window_disbands_stale_group(tmp_path):
    path, leader_guid, member_guid = _make_db(tmp_path)
    leader = FakeSession(leader_guid, "Leader")
    member = FakeSession(member_guid, "Member")
    server = FakeServer(leader, member)
    manager = server.groups
    manager.load(path)
    asyncio.run(manager.invite(leader, "Member"))
    asyncio.run(manager.accept(member))
    group = manager.group_for(leader_guid)

    member.in_world = False
    asyncio.run(manager.on_session_end(member_guid, member))
    leader.in_world = False
    asyncio.run(manager.on_session_end(leader_guid, leader))
    group.offline_since = int(time.time()) - manager.offline_expiry - 1

    leader.in_world = True
    server.sessions = [leader]
    asyncio.run(manager.on_player_login(leader))
    assert manager.group_for(leader_guid) is None


def test_group_list_uses_loot_method_master_and_threshold():
    leader = FakeSession(100, "Leader")
    member = FakeSession(200, "Member")
    manager, group = _make_group(leader, member)
    group.loot_method = group_packets.LOOT_METHOD_MASTER_LOOT
    group.master_looter_guid = 100
    group.loot_threshold = 4
    payload = group_packets.build_group_list(group, 100)
    # Skip the single remote member record to reach the settings block.
    pos = 4 + 16
    end = payload.index(b"\x00", pos)
    pos = end + 1 + 8 + 4
    pos += 8                              # leader guid
    assert payload[pos] == group_packets.LOOT_METHOD_MASTER_LOOT
    master, = struct.unpack_from("<Q", payload, pos + 1)
    assert master == 100
    assert payload[pos + 9] == 4


def test_need_greed_roll_awards_the_need_winner():
    leader = FakeSession(100, "Leader")
    member = FakeSession(200, "Member")
    manager, group = _make_group(leader, member)
    group.loot_method = group_packets.LOOT_METHOD_GROUP_LOOT
    group.loot_threshold = 2
    items = [(500, 1), (501, 1)]
    qualities = {500: 4, 501: 1}

    async def flow():
        blocked, master = await manager.begin_group_loot(
            group, 77, 0x0000000000000077, 0, items, qualities,
            [leader, member])
        assert blocked == {0} and master == set()
        roll = next(iter(group.rolls.values()))
        assert roll.item_entry == 500
        assert any(op == Opcodes.SMSG_LOOT_START_ROLL
                   for op, _ in leader.sent)
        # Both members pick up the roll popup; below-threshold slot is normal.
        assert manager.loot_slot_types(
            group, 77, items, qualities, 100)[0] \
            == group_packets.LOOT_SLOT_TYPE_ROLL_ONGOING
        assert 1 not in manager.loot_slot_types(
            group, 77, items, qualities, 100)
        await manager.cast_roll_vote(leader, roll.item_guid, roll.slot,
                                     group_packets.ROLL_NEED)
        assert leader.awards == []
        assert any(op == Opcodes.SMSG_LOOT_ROLL for op, _ in member.sent)
        await manager.cast_roll_vote(member, roll.item_guid, roll.slot,
                                     group_packets.ROLL_GREED)
        assert leader.awards == [(0x77, 77, 500, 1, 0)]
        assert member.awards == []

    asyncio.run(flow())


def test_all_passed_roll_leaves_item_lootable():
    leader = FakeSession(100, "Leader")
    member = FakeSession(200, "Member")
    manager, group = _make_group(leader, member)
    group.loot_method = group_packets.LOOT_METHOD_NEED_BEFORE_GREED
    items = [(500, 1)]
    qualities = {500: 3}

    async def flow():
        await manager.begin_group_loot(
            group, 88, 0x88, 0, items, qualities, [leader, member])
        roll = next(iter(group.rolls.values()))
        await manager.cast_roll_vote(leader, roll.item_guid, roll.slot,
                                     group_packets.ROLL_PASS)
        await manager.cast_roll_vote(member, roll.item_guid, roll.slot,
                                     group_packets.ROLL_PASS)
        assert not group.rolls
        assert leader.awards == [] and member.awards == []
        assert (88, 0) in group.all_passed
        assert any(op == Opcodes.SMSG_LOOT_ALL_PASSED
                   for op, _ in leader.sent)
        manager.forget_corpse(88)
        assert (88, 0) not in group.all_passed

    asyncio.run(flow())


def test_master_loot_threshold_and_give():
    leader = FakeSession(100, "Leader")
    member = FakeSession(200, "Member")
    manager, group = _make_group(leader, member)
    group.loot_method = group_packets.LOOT_METHOD_MASTER_LOOT
    group.master_looter_guid = 100
    group.loot_threshold = 3
    items = [(600, 1), (601, 1)]
    qualities = {600: 2, 601: 4}

    async def flow():
        blocked, master = await manager.begin_group_loot(
            group, 99, 99, 0, items, qualities, [leader, member])
        assert master == {1} and blocked == set()
        assert any(op == Opcodes.SMSG_LOOT_MASTER_LIST
                   for op, _ in leader.sent)
        leader_types = manager.loot_slot_types(
            group, 99, items, qualities, 100)
        member_types = manager.loot_slot_types(
            group, 99, items, qualities, 200)
        assert leader_types[1] == group_packets.LOOT_SLOT_TYPE_MASTER
        assert member_types[1] == group_packets.LOOT_SLOT_TYPE_LOCKED
        assert not manager.is_loot_slot_blocked(group, 99, 0)
        assert manager.is_loot_slot_blocked(group, 99, 1)
        assert await manager.master_loot_give(leader, 99, 1, 200)
        assert member.awards == [(99, 99, 200, 1, 1)]
        assert not manager.is_loot_slot_blocked(group, 99, 1)
        manager.forget_corpse(99)
        assert 99 not in group.master_slots

    asyncio.run(flow())


def test_ready_check_and_target_icons():
    leader = FakeSession(100, "Leader")
    member = FakeSession(200, "Member")
    manager, group = _make_group(leader, member)
    asyncio.run(manager.set_assistant(leader, 200, True))

    async def flow():
        await manager.start_ready_check(leader)
        for session in (leader, member):
            assert any(op == Opcodes.MSG_RAID_READY_CHECK
                       for op, _ in session.sent)
        leader.sent.clear()
        member.sent.clear()
        await manager.confirm_ready(member, 1)
        for session in (leader, member):
            payload = [data for op, data in session.sent
                       if op == Opcodes.MSG_RAID_READY_CHECK_CONFIRM]
            assert payload and struct.unpack_from("<Q", payload[0], 0)[0] == 200
            assert payload[0][8] == 1
        leader.sent.clear()
        member.sent.clear()
        await manager.finish_ready_check(leader)
        for session in (leader, member):
            assert any(op == Opcodes.MSG_RAID_READY_CHECK_FINISHED
                       for op, _ in session.sent)
        assert group.ready_check_deadline == 0.0

        # Party target icons: any member may mark; duplicates are cleared.
        await manager.set_target_icon(member, 0, 500)
        await manager.set_target_icon(member, 1, 500)
        assert group.target_icons == [0, 500, 0, 0, 0, 0, 0, 0]
        assert any(op == Opcodes.MSG_RAID_TARGET_UPDATE
                   for op, _ in leader.sent)

        # Raid icons are restricted to leader/assistant.
        group.raid = True
        group.members[200].member_flags = 0
        member.sent.clear()
        await manager.set_target_icon(member, 2, 700)
        assert group.target_icons[2] == 0

    asyncio.run(flow())


def test_roll_timer_passes_silent_members():
    leader = FakeSession(100, "Leader")
    member = FakeSession(200, "Member")
    manager, group = _make_group(leader, member)
    group.loot_method = group_packets.LOOT_METHOD_GROUP_LOOT
    items = [(500, 1)]
    qualities = {500: 3}

    async def flow():
        await manager.begin_group_loot(
            group, 55, 0x55, 0, items, qualities, [leader, member])
        roll = next(iter(group.rolls.values()))
        await manager.cast_roll_vote(leader, roll.item_guid, roll.slot,
                                     group_packets.ROLL_NEED)
        # Force the deadline and let the tick resolve the silent member as pass.
        roll.deadline = 0.0
        await manager.tick_rolls(1.0)
        assert not group.rolls
        assert leader.awards == [(0x55, 55, 500, 1, 0)]

    asyncio.run(flow())
