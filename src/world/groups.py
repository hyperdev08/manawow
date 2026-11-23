"""Session-scoped party/raid groups for the 3.3.5a server.

The real core persists groups in MySQL.  This server has no account-wide group
storage yet, so a group lives as long as at least one member is online.  The
combat/loot rules are the important part here: group members share loot rights,
split XP, and party-only spells no longer hit unrelated players.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional

from src.protocol.opcodes import Opcodes
from src.world import group_packets as packets

MAX_PARTY_SIZE = 5
MAX_RAID_SIZE = 40

_ALLIANCE_RACES = frozenset((1, 3, 4, 7, 11))
_HORDE_RACES = frozenset((2, 5, 6, 8, 10))


def _team(character) -> int:
    race = int((character or {}).get("race", 0) or 0)
    if race in _ALLIANCE_RACES:
        return 1
    if race in _HORDE_RACES:
        return 2
    return 0


@dataclass
class GroupMember:
    guid: int
    name: str
    subgroup: int = 0
    joined_at: float = field(default_factory=time.monotonic)


class Group:
    def __init__(self, manager, group_id: int, leader_guid: int,
                 leader_name: str):
        self.manager = manager
        self.id = int(group_id)
        self.guid = (0x1F << 56) | (self.id & 0xFFFFFFFF)
        self.leader_guid = int(leader_guid)
        self.raid = False
        self.members: dict[int, GroupMember] = {}
        self.invites: set[int] = set()
        self._counter = 0

    def next_counter(self) -> int:
        self._counter += 1
        return self._counter

    @property
    def size(self) -> int:
        return len(self.members)

    @property
    def capacity(self) -> int:
        return MAX_RAID_SIZE if self.raid else MAX_PARTY_SIZE

    def is_member(self, guid: int) -> bool:
        return int(guid) in self.members

    def is_leader(self, guid: int) -> bool:
        return int(guid) == self.leader_guid


class GroupManager:
    def __init__(self, server=None):
        self.server = server
        self.groups: dict[int, Group] = {}
        self.by_player: dict[int, Group] = {}
        self.invites: dict[int, Group] = {}
        self._next_id = 1
        self.loot_locks: dict[int, int] = {}

    # -- session helpers ----------------------------------------------
    def session_for(self, guid: int):
        guid = int(guid or 0)
        for session in getattr(self.server, "sessions", ()) if self.server else ():
            if getattr(session, "player_guid", None) == guid:
                return session
        return None

    def session_by_name(self, name: str):
        wanted = str(name or "").casefold()
        for session in getattr(self.server, "sessions", ()) if self.server else ():
            character = getattr(session, "character", None)
            if character and str(character.get("name", "")).casefold() == wanted:
                return session
        return None

    def group_for(self, guid: int) -> Optional[Group]:
        return self.by_player.get(int(guid or 0))

    def same_group(self, first: int, second: int) -> bool:
        group = self.by_player.get(int(first or 0))
        return group is not None and group.is_member(second)

    def members(self, group: Group) -> list[GroupMember]:
        return list(group.members.values())

    def member_sessions(self, group: Group, map_id: Optional[int] = None):
        result = []
        for member in group.members.values():
            session = self.session_for(member.guid)
            if session is None or not getattr(session, "in_world", False):
                continue
            if map_id is not None and int(
                    (session.character or {}).get("map", 0)) != int(map_id):
                continue
            result.append(session)
        return result

    def group_member_sessions(self, guid: int, map_id: Optional[int] = None):
        group = self.group_for(guid)
        return self.member_sessions(group, map_id) if group else []

    # -- packet helpers ------------------------------------------------
    async def send_group_list(self, group: Group) -> None:
        for session in self.member_sessions(group):
            await session.send_packet(
                Opcodes.SMSG_GROUP_LIST,
                packets.build_group_list(group, int(session.player_guid)))

    async def send_empty_group_list(self, session) -> None:
        await session.send_packet(Opcodes.SMSG_GROUP_LIST,
                                  packets.build_group_list_empty())

    async def send_member_stats(self, group: Group) -> None:
        sessions = self.member_sessions(group)
        for source in sessions:
            payload = packets.build_party_member_stats_full(source)
            for target in sessions:
                await target.send_packet(
                    Opcodes.SMSG_PARTY_MEMBER_STATS_FULL, payload)

    async def send_party_result(self, session, operation: int, name: str,
                                result: int) -> None:
        await session.send_packet(
            Opcodes.SMSG_PARTY_COMMAND_RESULT,
            packets.build_party_command_result(operation, name, result))

    # -- lifecycle -----------------------------------------------------
    async def invite(self, leader_session, target_name: str) -> None:
        if leader_session is None or not leader_session.character:
            return
        target = self.session_by_name(target_name)
        name = str(target_name or "")
        if target is None or target is leader_session:
            await self.send_party_result(
                leader_session, 0, name, packets.ERR_BAD_PLAYER_NAME_S)
            return
        leader_team = _team(leader_session.character)
        target_team = _team(target.character)
        if leader_team and target_team and leader_team != target_team:
            await self.send_party_result(
                leader_session, 0, target.character["name"],
                packets.ERR_PLAYER_WRONG_FACTION)
            return
        if self.group_for(target.player_guid) is not None \
                or int(target.player_guid) in self.invites:
            await self.send_party_result(
                leader_session, 0, target.character["name"],
                packets.ERR_ALREADY_IN_GROUP_S)
            return
        group = self.group_for(leader_session.player_guid)
        if group is None:
            group = self._create(leader_session)
            await self.send_group_list(group)
        if group.size >= group.capacity:
            await self.send_party_result(
                leader_session, 0, target.character["name"],
                packets.ERR_GROUP_FULL)
            return
        group.invites.add(int(target.player_guid))
        self.invites[int(target.player_guid)] = group
        await target.send_packet(
            Opcodes.SMSG_GROUP_INVITE,
            packets.build_group_invite(str(leader_session.character["name"])))
        await self.send_party_result(
            leader_session, 0, target.character["name"],
            packets.ERR_PARTY_RESULT_OK)

    def _create(self, leader_session) -> Group:
        group = Group(self, self._next_id,
                      int(leader_session.player_guid),
                      str(leader_session.character["name"]))
        self._next_id += 1
        self.groups[group.id] = group
        group.members[int(leader_session.player_guid)] = GroupMember(
            int(leader_session.player_guid),
            str(leader_session.character["name"]))
        self.by_player[int(leader_session.player_guid)] = group
        return group

    async def accept(self, session) -> None:
        if session is None or not session.character:
            return
        group = self.invites.pop(int(session.player_guid), None)
        if group is None:
            return
        group.invites.discard(int(session.player_guid))
        if group.size >= group.capacity:
            await self.send_party_result(
                session, 0, "", packets.ERR_GROUP_FULL)
            return
        group.members[int(session.player_guid)] = GroupMember(
            int(session.player_guid), str(session.character["name"]))
        self.by_player[int(session.player_guid)] = group
        await self.send_group_list(group)
        await self.send_member_stats(group)

    async def decline(self, session) -> None:
        if session is None:
            return
        group = self.invites.pop(int(session.player_guid), None)
        if group is None:
            return
        group.invites.discard(int(session.player_guid))
        leader = self.session_for(group.leader_guid)
        if leader is not None:
            await leader.send_packet(
                Opcodes.SMSG_GROUP_DECLINE,
                packets.build_group_decline(str(session.character["name"])))
        await self._drop_solo_group(group)

    async def _drop_solo_group(self, group: Group) -> None:
        if group.size <= 1 and not group.invites:
            sessions = self.member_sessions(group)
            await self.disband(group, notify=False)
            for session in sessions:
                await self.send_empty_group_list(session)

    async def leave(self, session_or_guid) -> None:
        guid = (int(session_or_guid.player_guid)
                if hasattr(session_or_guid, "player_guid")
                else int(session_or_guid or 0))
        group = self.group_for(guid)
        if group is None:
            return
        session = self.session_for(guid)
        group.members.pop(guid, None)
        self.by_player.pop(guid, None)
        if session is not None:
            await self.send_empty_group_list(session)
        if not group.members:
            await self.disband(group, notify=False)
            return
        if group.is_leader(guid):
            new_leader = next(iter(group.members.values()))
            group.leader_guid = int(new_leader.guid)
            for target in self.member_sessions(group):
                await target.send_packet(
                    Opcodes.SMSG_GROUP_SET_LEADER,
                    packets.build_group_set_leader(new_leader.name))
        if group.size <= 1 and not group.invites:
            await self.disband(group)
            return
        await self.send_group_list(group)
        await self.send_member_stats(group)

    async def disband(self, group: Group, notify: bool = True) -> None:
        sessions = self.member_sessions(group)
        for member in list(group.members):
            self.by_player.pop(int(member), None)
        for invited in list(group.invites):
            self.invites.pop(int(invited), None)
        group.members.clear()
        group.invites.clear()
        self.groups.pop(group.id, None)
        if notify:
            for session in sessions:
                await session.send_packet(Opcodes.SMSG_GROUP_DESTROYED, b"")
                await self.send_empty_group_list(session)

    async def uninvite(self, leader_session, target_guid: int = 0,
                       target_name: str = "") -> None:
        group = self.group_for(leader_session.player_guid)
        if group is None or not group.is_leader(leader_session.player_guid):
            await self.send_party_result(
                leader_session, 1, target_name, packets.ERR_NOT_LEADER)
            return
        target = None
        if target_guid:
            target = self.session_for(target_guid)
        elif target_name:
            target = self.session_by_name(target_name)
        guid = int(target.player_guid) if target is not None else int(target_guid)
        if guid and group.is_member(guid):
            await self.leave(guid)
            return
        if guid in group.invites:
            group.invites.discard(guid)
            self.invites.pop(guid, None)
            if target is not None:
                await target.send_packet(
                    Opcodes.SMSG_GROUP_UNINVITE,
                    packets.build_group_uninvite(""))
            await self.send_party_result(
                leader_session, 1, target_name, packets.ERR_PARTY_RESULT_OK)
            return
        await self.send_party_result(
            leader_session, 1, target_name,
            packets.ERR_TARGET_NOT_IN_GROUP_S)

    async def set_leader(self, session, target_guid: int) -> None:
        group = self.group_for(session.player_guid)
        if group is None or not group.is_leader(session.player_guid) \
                or not group.is_member(target_guid):
            return
        member = group.members[int(target_guid)]
        group.leader_guid = int(target_guid)
        for target in self.member_sessions(group):
            await target.send_packet(
                Opcodes.SMSG_GROUP_SET_LEADER,
                packets.build_group_set_leader(member.name))
        await self.send_group_list(group)

    async def convert_raid(self, session) -> None:
        group = self.group_for(session.player_guid)
        if group is None or not group.is_leader(session.player_guid):
            return
        if group.size < 2:
            await self.send_party_result(
                session, 0, "", packets.ERR_GROUP_FULL)
            return
        group.raid = True
        for index, member in enumerate(group.members.values()):
            member.subgroup = min(7, index // 5)
        await self.send_group_list(group)
        await self.send_member_stats(group)

    async def change_subgroup(self, session, target_name: str,
                              subgroup: int) -> None:
        group = self.group_for(session.player_guid)
        if group is None or not group.is_leader(session.player_guid):
            return
        target = self.session_by_name(target_name)
        if target is None or not group.is_member(target.player_guid):
            return
        group.members[int(target.player_guid)].subgroup = (
            int(subgroup) & 7)
        await self.send_group_list(group)

    async def request_member_stats(self, session, target_guid: int) -> None:
        group = self.group_for(session.player_guid)
        if group is None or not group.is_member(target_guid):
            return
        target = self.session_for(target_guid)
        if target is None:
            return
        await session.send_packet(
            Opcodes.SMSG_PARTY_MEMBER_STATS_FULL,
            packets.build_party_member_stats_full(target))

    async def on_session_end(self, player_guid: int) -> None:
        guid = int(player_guid or 0)
        if not guid:
            return
        self.loot_locks = {
            target: owner for target, owner in self.loot_locks.items()
            if owner != guid
        }
        if guid in self.invites:
            group = self.invites.pop(guid)
            group.invites.discard(guid)
            await self._drop_solo_group(group)
        if self.group_for(guid) is not None:
            await self.leave(guid)

    # -- loot serialization -------------------------------------------
    def acquire_loot(self, low_guid: int, player_guid: int) -> bool:
        low_guid = int(low_guid)
        player_guid = int(player_guid or 0)
        owner = self.loot_locks.get(low_guid)
        if owner is not None and owner != player_guid:
            return False
        self.loot_locks[low_guid] = player_guid
        return True

    def release_loot(self, low_guid: int, player_guid: int) -> None:
        low_guid = int(low_guid)
        if self.loot_locks.get(low_guid) == int(player_guid or 0):
            self.loot_locks.pop(low_guid, None)
