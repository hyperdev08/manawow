"""Persistent party/raid groups for the 3.3.5a server.

Groups and their members are stored in the character database (``groups`` and
``group_members``), so a group survives a logout or a server restart and its
offline members are still shown in the group list.  The manager also owns the
group loot rules (need/greed/pass rolls, master loot and the loot threshold),
raid target icons and ready checks.
"""
from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from src.common.db_io import run_db_write
from src.protocol.opcodes import Opcodes
from src.world import character_db
from src.world import creature
from src.world import group_packets as packets
from src.world.group_packets import (
    LOOT_METHOD_FREE_FOR_ALL,
    LOOT_METHOD_GROUP_LOOT,
    LOOT_METHOD_MASTER_LOOT,
    LOOT_METHOD_NEED_BEFORE_GREED,
    LOOT_METHOD_ROUND_ROBIN,
    LOOT_SLOT_TYPE_LOCKED,
    LOOT_SLOT_TYPE_MASTER,
    LOOT_SLOT_TYPE_ROLL_ONGOING,
    ROLL_FLAG_TYPE_GREED,
    ROLL_FLAG_TYPE_NEED,
    ROLL_FLAG_TYPE_PASS,
    ROLL_GREED,
    ROLL_NEED,
    ROLL_PASS,
)

MAX_PARTY_SIZE = 5
MAX_RAID_SIZE = 40
ROLL_TIMEOUT = 60.0
READY_CHECK_TIMEOUT = 30.0
LOOT_ROLL_COUNTDOWN_MS = 60000
# 3.3.5a keeps a party/raid that has gone fully offline for a short reconnect
# window, then disbands it.  Five minutes matches the retail behaviour.
OFFLINE_GROUP_EXPIRY = 5 * 60
OFFLINE_GROUP_SCAN_INTERVAL = 5.0

# Roll::PlayerVote (TrinityCore).
NOT_EMITED_YET = 4
NOT_VALID = 5

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
    member_flags: int = 0
    roles: int = 0
    joined_at: float = field(default_factory=time.monotonic)


@dataclass
class LootRoll:
    """One in-progress group roll for a corpse loot slot."""

    item_guid: int
    loot_target: int
    low_guid: int
    slot: int
    item_entry: int
    count: int
    quality: int
    map_id: int
    deadline: float
    votes: Dict[int, int] = field(default_factory=dict)
    total_players: int = 0
    total_need: int = 0
    total_pass: int = 0
    total_greed: int = 0
    random_suffix: int = 0
    random_property: int = 0


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
        self.loot_method = LOOT_METHOD_GROUP_LOOT
        self.master_looter_guid = 0
        self.loot_threshold = 2
        self.target_icons: List[int] = [0] * 8
        self.rolls: Dict[int, LootRoll] = {}
        self.roll_started: set[tuple[int, int]] = set()
        self.all_passed: set[tuple[int, int]] = set()
        self.master_slots: Dict[int, set[int]] = {}
        self.ready_check_deadline = 0.0
        self.offline_since = 0
        if leader_guid:
            self.members[int(leader_guid)] = GroupMember(
                int(leader_guid), str(leader_name))

    def next_counter(self) -> int:
        self._counter = (self._counter + 1) & 0xFFFFFFFF
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

    def is_assistant(self, guid: int) -> bool:
        member = self.members.get(int(guid))
        return bool(member and member.member_flags
                    & packets.MEMBER_FLAG_ASSISTANT)

    def can_manage(self, guid: int) -> bool:
        return self.is_leader(guid) or self.is_assistant(guid)

    def member_named(self, name: str) -> Optional[GroupMember]:
        wanted = str(name or "").casefold()
        for member in self.members.values():
            if member.name.casefold() == wanted:
                return member
        return None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "leader_guid": self.leader_guid,
            "raid": self.raid,
            "loot_method": self.loot_method,
            "master_looter_guid": self.master_looter_guid,
            "loot_threshold": self.loot_threshold,
            "target_icons": list(self.target_icons),
            "counter": self._counter,
            "offline_since": int(self.offline_since or 0),
            "members": [
                {"guid": member.guid, "name": member.name,
                 "subgroup": member.subgroup,
                 "member_flags": member.member_flags,
                 "roles": member.roles}
                for member in self.members.values()
            ],
        }


class GroupManager:
    def __init__(self, server=None):
        self.server = server
        self.groups: dict[int, Group] = {}
        self.by_player: dict[int, Group] = {}
        self.invites: dict[int, Group] = {}
        self._next_id = 1
        # Roll item GUIDs share the Item high-guid, so keep their low part in
        # a range that cannot collide with the small character_items row ids.
        self._next_item_guid = 0x40000000
        self.loot_locks: dict[int, int] = {}
        # Character database path; when set every change is persisted.
        self.path: Optional[str] = None
        self.loaded = False
        self.offline_expiry = OFFLINE_GROUP_EXPIRY
        self._last_offline_scan = 0
        try:
            configured = getattr(
                getattr(getattr(server, "config", None), "worldserver", None),
                "group_offline_expiry", OFFLINE_GROUP_EXPIRY)
            self.offline_expiry = max(0, int(configured))
        except (TypeError, ValueError):
            self.offline_expiry = OFFLINE_GROUP_EXPIRY

    # -- persistence ---------------------------------------------------
    def load(self, path: Optional[str]) -> None:
        """Load persisted groups from the character database."""
        self.path = str(path) if path else None
        self.loaded = True
        self.groups.clear()
        self.by_player.clear()
        self.invites.clear()
        if not self.path:
            return
        try:
            rows = character_db.load_groups(self.path)
        except Exception:
            rows = []
        max_id = 0
        for row in rows:
            group = Group(self, int(row["id"]),
                          int(row.get("leader_guid") or 0), "")
            group.members.clear()
            group.raid = bool(row.get("raid"))
            group.loot_method = int(row.get("loot_method") or 0)
            group.master_looter_guid = int(
                row.get("master_looter_guid") or 0)
            group.loot_threshold = int(row.get("loot_threshold") or 2)
            icons = list(row.get("target_icons") or [])
            group.target_icons = [
                int(icons[index]) if index < len(icons) else 0
                for index in range(8)]
            group._counter = int(row.get("counter") or 0)
            group.offline_since = int(row.get("offline_since") or 0)
            for member in row.get("members") or []:
                guid = int(member["guid"])
                group.members[guid] = GroupMember(
                    guid, str(member.get("name") or f"Player-{guid}"),
                    int(member.get("subgroup") or 0) & 7,
                    int(member.get("member_flags") or 0) & 0xFF,
                    int(member.get("roles") or 0) & 0xFF)
                self.by_player[guid] = group
            if not group.members:
                continue
            if group.leader_guid not in group.members:
                group.leader_guid = next(iter(group.members))
            self.groups[group.id] = group
            max_id = max(max_id, group.id)
        self._next_id = max_id + 1

    async def _persist(self, group: Group) -> None:
        if not self.path:
            return
        data = group.to_dict()
        try:
            await run_db_write(character_db.save_group, self.path, data)
        except Exception:
            pass

    async def _delete_persisted(self, group: Group) -> None:
        if not self.path:
            return
        try:
            await run_db_write(
                character_db.delete_group, self.path, group.id)
        except Exception:
            pass

    # -- session helpers ----------------------------------------------
    def session_for(self, guid: int):
        guid = int(guid or 0)
        fallback = None
        for session in getattr(self.server, "sessions", ()) if self.server else ():
            if getattr(session, "player_guid", None) != guid:
                continue
            if getattr(session, "in_world", False):
                return session
            fallback = fallback or session
        return fallback

    def session_by_name(self, name: str):
        wanted = str(name or "").casefold()
        fallback = None
        for session in getattr(self.server, "sessions", ()) if self.server else ():
            character = getattr(session, "character", None)
            if not character:
                continue
            if str(character.get("name", "")).casefold() != wanted:
                continue
            if getattr(session, "in_world", False):
                return session
            fallback = fallback or session
        return fallback

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

    async def send_target_icons(self, session, group: Group) -> None:
        await session.send_packet(
            Opcodes.MSG_RAID_TARGET_UPDATE,
            packets.build_raid_target_update_list(group.target_icons))

    async def on_player_login(self, session) -> None:
        """Re-attach a returning member to its persisted group."""
        guid = int(getattr(session, "player_guid", 0) or 0)
        group = self.group_for(guid)
        if group is None:
            return
        if group.offline_since:
            now = int(time.time())
            if (self.offline_expiry > 0
                    and now - int(group.offline_since)
                    >= self.offline_expiry):
                # The reconnect window elapsed while nobody was online to run
                # the periodic scan.
                await self.disband(group, notify=False)
                return
            group.offline_since = 0
            await self._persist(group)
        await self.send_group_list(group)
        await self.send_member_stats(group)
        await self.send_target_icons(session, group)

    # -- lifecycle -----------------------------------------------------
    async def invite(self, leader_session, target_name: str) -> None:
        if leader_session is None or not leader_session.character:
            return
        target = self.session_by_name(target_name)
        name = str(target_name or "")
        if target is None or target is leader_session \
                or not getattr(target, "in_world", False):
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
            group = await self._create(leader_session)
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

    async def _create(self, leader_session) -> Group:
        group = Group(self, self._next_id,
                      int(leader_session.player_guid),
                      str(leader_session.character["name"]))
        self._next_id += 1
        self.groups[group.id] = group
        self.by_player[int(leader_session.player_guid)] = group
        await self._persist(group)
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
        group.offline_since = 0
        await self._persist(group)
        await self.send_group_list(group)
        await self.send_member_stats(group)
        await self.send_target_icons(session, group)

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
        self.loot_locks = {
            target: owner for target, owner in self.loot_locks.items()
            if owner != guid
        }
        self._drop_roll_votes(group, guid)
        if session is not None:
            await self.send_empty_group_list(session)
        if not group.members:
            await self.disband(group, notify=False)
            return
        if group.is_leader(guid):
            online = [member for member in group.members.values()
                      if self.session_for(member.guid) is not None]
            new_leader = (online[0] if online
                          else next(iter(group.members.values())))
            group.leader_guid = int(new_leader.guid)
            if group.master_looter_guid == guid:
                group.master_looter_guid = int(new_leader.guid)
            for target in self.member_sessions(group):
                await target.send_packet(
                    Opcodes.SMSG_GROUP_SET_LEADER,
                    packets.build_group_set_leader(new_leader.name))
        if group.size <= 1 and not group.invites:
            await self.disband(group)
            return
        await self._persist(group)
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
        group.rolls.clear()
        self.groups.pop(group.id, None)
        await self._delete_persisted(group)
        if notify:
            for session in sessions:
                await session.send_packet(Opcodes.SMSG_GROUP_DESTROYED, b"")
                await self.send_empty_group_list(session)

    async def uninvite(self, leader_session, target_guid: int = 0,
                       target_name: str = "") -> None:
        group = self.group_for(leader_session.player_guid)
        if group is None or not group.can_manage(leader_session.player_guid):
            await self.send_party_result(
                leader_session, 1, target_name, packets.ERR_NOT_LEADER)
            return
        target = None
        if target_guid:
            target = self.session_for(target_guid)
        elif target_name:
            target = self.session_by_name(target_name)
        guid = int(target.player_guid) if target is not None else int(target_guid)
        if not guid and target_name:
            member = group.member_named(target_name)
            guid = int(member.guid) if member is not None else 0
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
        await self._persist(group)
        for target in self.member_sessions(group):
            await target.send_packet(
                Opcodes.SMSG_GROUP_SET_LEADER,
                packets.build_group_set_leader(member.name))
        await self.send_group_list(group)

    async def set_assistant(self, session, target_guid: int,
                            apply: bool) -> None:
        group = self.group_for(session.player_guid)
        if group is None or not group.is_leader(session.player_guid):
            return
        member = group.members.get(int(target_guid))
        if member is None:
            return
        if apply:
            member.member_flags |= packets.MEMBER_FLAG_ASSISTANT
        else:
            member.member_flags &= ~packets.MEMBER_FLAG_ASSISTANT
        await self._persist(group)
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
        await self._persist(group)
        await self.send_group_list(group)
        await self.send_member_stats(group)

    async def change_subgroup(self, session, target_name: str,
                              subgroup: int) -> None:
        group = self.group_for(session.player_guid)
        if group is None or not group.can_manage(session.player_guid):
            return
        target = self.session_by_name(target_name)
        if target is None or not group.is_member(target.player_guid):
            member = group.member_named(target_name)
            if member is None:
                return
            target_guid = int(member.guid)
        else:
            target_guid = int(target.player_guid)
        group.members[target_guid].subgroup = (
            int(subgroup) & 7)
        await self._persist(group)
        await self.send_group_list(group)

    async def swap_subgroup(self, session, first_name: str,
                            second_name: str) -> None:
        group = self.group_for(session.player_guid)
        if group is None or not group.can_manage(session.player_guid):
            return
        first = group.member_named(first_name)
        second = group.member_named(second_name)
        if first is None or second is None:
            return
        first.subgroup, second.subgroup = (
            second.subgroup, first.subgroup)
        await self._persist(group)
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

    # -- loot settings -------------------------------------------------
    async def set_loot_method(self, session, method: int,
                              master_guid: int, threshold: int) -> None:
        group = self.group_for(session.player_guid)
        if group is None or not group.is_leader(session.player_guid):
            return
        method = int(method)
        threshold = int(threshold)
        if method not in (LOOT_METHOD_FREE_FOR_ALL,
                          LOOT_METHOD_ROUND_ROBIN,
                          LOOT_METHOD_MASTER_LOOT,
                          LOOT_METHOD_GROUP_LOOT,
                          LOOT_METHOD_NEED_BEFORE_GREED):
            return
        if threshold < 2 or threshold > 6:
            return
        if method == LOOT_METHOD_MASTER_LOOT \
                and not group.is_member(master_guid):
            return
        group.loot_method = method
        group.master_looter_guid = int(master_guid or 0)
        group.loot_threshold = threshold
        await self._persist(group)
        await self.send_group_list(group)

    async def set_pass_on_loot(self, session, enabled: bool) -> None:
        combat = session._get_combat_state()
        combat["pass_on_group_loot"] = bool(enabled)

    # -- group loot rolls ----------------------------------------------
    def _new_item_guid(self) -> int:
        guid = 0x4000000000000000 | (self._next_item_guid & 0xFFFFFFFF)
        self._next_item_guid += 1
        return guid

    def _roll_for(self, group: Group, low_guid: int,
                  slot: int) -> Optional[LootRoll]:
        for roll in group.rolls.values():
            if roll.low_guid == int(low_guid) and roll.slot == int(slot):
                return roll
        return None

    def _participants(self, group: Group, eligible_sessions):
        result = []
        for session in eligible_sessions or []:
            guid = int(getattr(session, "player_guid", 0) or 0)
            if guid and group.is_member(guid) and session not in result:
                result.append(session)
        return result

    async def begin_group_loot(self, group: Group, low_guid: int,
                               loot_target: int, map_id: int, items,
                               qualities: Dict[int, int],
                               eligible_sessions) -> tuple[set[int], set[int]]:
        """Apply the group's loot rules to a freshly opened corpse.

        Returns ``(blocked_slots, master_slots)``.  ``blocked_slots`` are
        hidden behind an ongoing roll; ``master_slots`` must be distributed by
        the master looter.
        """
        blocked: set[int] = set()
        master: set[int] = set()
        low_guid = int(low_guid)
        if group.loot_method == LOOT_METHOD_MASTER_LOOT:
            slots = {
                slot for slot, (entry, _count) in enumerate(items)
                if int(qualities.get(int(entry), 0) or 0)
                >= group.loot_threshold
            }
            group.master_slots[low_guid] = slots
            master |= slots
            if slots:
                participants = self._participants(
                    group, eligible_sessions)
                payload = packets.build_loot_master_list(
                    [int(session.player_guid) for session in participants])
                for session in participants:
                    await session.send_packet(
                        Opcodes.SMSG_LOOT_MASTER_LIST, payload)
            return blocked, master

        if group.loot_method not in (LOOT_METHOD_GROUP_LOOT,
                                     LOOT_METHOD_NEED_BEFORE_GREED):
            return blocked, master

        now = time.monotonic()
        participants = self._participants(group, eligible_sessions)
        for slot, (entry, count) in enumerate(items):
            key = (low_guid, slot)
            if key in group.roll_started:
                if self._roll_for(group, low_guid, slot) is not None:
                    blocked.add(slot)
                continue
            if key in group.all_passed:
                continue
            quality = int(qualities.get(int(entry), 0) or 0)
            if quality < group.loot_threshold:
                continue
            if not participants:
                group.all_passed.add(key)
                continue
            votes: Dict[int, int] = {}
            pass_count = 0
            for session in participants:
                guid = int(session.player_guid)
                passing = False
                try:
                    passing = bool(session._get_combat_state().get(
                        "pass_on_group_loot"))
                except Exception:
                    passing = False
                votes[guid] = ROLL_PASS if passing else NOT_EMITED_YET
                if passing:
                    pass_count += 1
            group.roll_started.add(key)
            if pass_count == len(participants):
                for session in participants:
                    if votes[int(session.player_guid)] == ROLL_PASS:
                        await session.send_packet(
                            Opcodes.SMSG_LOOT_ROLL,
                            packets.build_loot_roll(
                                0, slot, int(session.player_guid),
                                int(entry), 128, ROLL_PASS,
                                auto_pass=True))
                group.all_passed.add(key)
                continue
            roll = LootRoll(
                item_guid=self._new_item_guid(),
                loot_target=int(loot_target),
                low_guid=low_guid,
                slot=slot,
                item_entry=int(entry),
                count=int(count),
                quality=quality,
                map_id=int(map_id),
                deadline=now + ROLL_TIMEOUT,
                votes=votes,
                total_players=len(participants),
                total_pass=pass_count)
            group.rolls[roll.item_guid] = roll
            blocked.add(slot)
            for session in participants:
                guid = int(session.player_guid)
                if votes[guid] == ROLL_PASS:
                    await session.send_packet(
                        Opcodes.SMSG_LOOT_ROLL,
                        packets.build_loot_roll(
                            0, slot, guid, int(entry), 128, ROLL_PASS,
                            auto_pass=True))
            for session in participants:
                if votes[int(session.player_guid)] == NOT_EMITED_YET:
                    await session.send_packet(
                        Opcodes.SMSG_LOOT_START_ROLL,
                        packets.build_loot_start_roll(
                            roll.item_guid, int(map_id), slot, int(entry),
                            count=int(count),
                            countdown=LOOT_ROLL_COUNTDOWN_MS,
                            vote_mask=(ROLL_FLAG_TYPE_PASS
                                       | ROLL_FLAG_TYPE_NEED
                                       | ROLL_FLAG_TYPE_GREED)))
        return blocked, master

    def loot_slot_types(self, group: Optional[Group], low_guid: int, items,
                        qualities: Dict[int, int],
                        viewer_guid: int) -> Dict[int, int]:
        result: Dict[int, int] = {}
        if group is None:
            return result
        low_guid = int(low_guid)
        if group.loot_method == LOOT_METHOD_MASTER_LOOT:
            master_slots = group.master_slots.get(low_guid, set())
            for slot in master_slots:
                if slot < len(items):
                    result[slot] = (
                        LOOT_SLOT_TYPE_MASTER
                        if int(viewer_guid) == int(group.master_looter_guid)
                        else LOOT_SLOT_TYPE_LOCKED)
        for slot, (entry, _count) in enumerate(items):
            if self._roll_for(group, low_guid, slot) is not None:
                result[slot] = LOOT_SLOT_TYPE_ROLL_ONGOING
        return result

    def is_loot_slot_blocked(self, group: Optional[Group], low_guid: int,
                             slot: int) -> bool:
        if group is None:
            return False
        low_guid, slot = int(low_guid), int(slot)
        if self._roll_for(group, low_guid, slot) is not None:
            return True
        if group.loot_method == LOOT_METHOD_MASTER_LOOT:
            return slot in group.master_slots.get(low_guid, set())
        return False

    async def _send_to_voters(self, group: Group, roll: LootRoll,
                              opcode: int, payload: bytes) -> None:
        for guid, vote in list(roll.votes.items()):
            if vote == NOT_VALID:
                continue
            session = self.session_for(guid)
            if session is not None:
                await session.send_packet(opcode, payload)

    async def cast_roll_vote(self, session, item_guid: int, slot: int,
                             roll_type: int) -> None:
        group = self.group_for(session.player_guid)
        if group is None:
            return
        roll = group.rolls.get(int(item_guid))
        if roll is None:
            return
        guid = int(session.player_guid)
        current = roll.votes.get(guid)
        if current is None or current != NOT_EMITED_YET:
            return
        roll_type = int(roll_type)
        if roll_type == ROLL_PASS:
            roll.votes[guid] = ROLL_PASS
            roll.total_pass += 1
            await self._send_to_voters(
                group, roll, Opcodes.SMSG_LOOT_ROLL,
                packets.build_loot_roll(
                    0, roll.slot, guid, roll.item_entry, 128, ROLL_PASS))
        elif roll_type == ROLL_NEED:
            roll.votes[guid] = ROLL_NEED
            roll.total_need += 1
            await self._send_to_voters(
                group, roll, Opcodes.SMSG_LOOT_ROLL,
                packets.build_loot_roll(
                    0, roll.slot, guid, roll.item_entry, 0, 0))
        elif roll_type == ROLL_GREED:
            roll.votes[guid] = ROLL_GREED
            roll.total_greed += 1
            await self._send_to_voters(
                group, roll, Opcodes.SMSG_LOOT_ROLL,
                packets.build_loot_roll(
                    0, roll.slot, guid, roll.item_entry, 128, ROLL_GREED))
        else:
            return
        if (roll.total_pass + roll.total_need + roll.total_greed
                >= roll.total_players):
            await self._resolve_roll(group, roll)

    async def _resolve_roll(self, group: Group, roll: LootRoll) -> None:
        group.rolls.pop(roll.item_guid, None)
        need_guids = [guid for guid, vote in roll.votes.items()
                      if vote == ROLL_NEED and self.session_for(guid) is not None]
        greed_guids = [guid for guid, vote in roll.votes.items()
                       if vote == ROLL_GREED and self.session_for(guid) is not None]
        winner = None
        winner_roll = 0
        winner_type = ROLL_NEED
        if need_guids:
            for guid in need_guids:
                value = random.randint(1, 100)
                await self._send_to_voters(
                    group, roll, Opcodes.SMSG_LOOT_ROLL,
                    packets.build_loot_roll(
                        0, roll.slot, guid, roll.item_entry, value,
                        ROLL_NEED))
                if value > winner_roll:
                    winner, winner_roll = guid, value
        elif greed_guids:
            winner_type = ROLL_GREED
            for guid in greed_guids:
                value = random.randint(1, 100)
                await self._send_to_voters(
                    group, roll, Opcodes.SMSG_LOOT_ROLL,
                    packets.build_loot_roll(
                        0, roll.slot, guid, roll.item_entry, value,
                        ROLL_GREED))
                if value > winner_roll:
                    winner, winner_roll = guid, value
        if winner is None:
            group.all_passed.add((roll.low_guid, roll.slot))
            await self._send_to_voters(
                group, roll, Opcodes.SMSG_LOOT_ALL_PASSED,
                packets.build_loot_all_passed(
                    roll.item_guid, roll.slot, roll.item_entry))
            return
        await self._send_to_voters(
            group, roll, Opcodes.SMSG_LOOT_ROLL_WON,
            packets.build_loot_roll_won(
                0, roll.slot, roll.item_entry, winner, winner_roll,
                winner_type))
        session = self.session_for(winner)
        awarded = False
        if session is not None and hasattr(session, "_award_rolled_loot"):
            try:
                awarded = bool(await session._award_rolled_loot(
                    roll.loot_target, roll.low_guid, roll.item_entry,
                    roll.count, roll.slot))
            except Exception:
                awarded = False
        if not awarded:
            # Winner could not store it: leave the item lootable by hand.
            group.all_passed.add((roll.low_guid, roll.slot))

    async def tick_rolls(self, now: float) -> None:
        for group in list(self.groups.values()):
            for roll in list(group.rolls.values()):
                if roll.deadline > now:
                    continue
                for guid, vote in list(roll.votes.items()):
                    if vote == NOT_EMITED_YET:
                        roll.votes[guid] = ROLL_PASS
                        roll.total_pass += 1
                await self._resolve_roll(group, roll)

    def forget_corpse(self, low_guid: int) -> None:
        """Drop roll state for a corpse that is respawning/reused."""
        low_guid = int(low_guid)
        for group in self.groups.values():
            group.roll_started = {
                key for key in group.roll_started if key[0] != low_guid}
            group.all_passed = {
                key for key in group.all_passed if key[0] != low_guid}
            group.master_slots.pop(low_guid, None)
            for item_guid, roll in list(group.rolls.items()):
                if roll.low_guid == low_guid:
                    group.rolls.pop(item_guid, None)

    def _drop_roll_votes(self, group: Group, guid: int) -> None:
        for roll in list(group.rolls.values()):
            vote = roll.votes.pop(int(guid), None)
            if vote is None:
                continue
            if vote == ROLL_NEED:
                roll.total_need = max(0, roll.total_need - 1)
            elif vote == ROLL_GREED:
                roll.total_greed = max(0, roll.total_greed - 1)
            elif vote == ROLL_PASS:
                roll.total_pass = max(0, roll.total_pass - 1)
            roll.total_players = max(0, roll.total_players - 1)
            if (roll.total_need + roll.total_greed + roll.total_pass
                    >= roll.total_players):
                # Resolve on the next tick; this method is synchronous.
                roll.deadline = 0.0

    async def master_loot_give(self, session, loot_target: int, slot: int,
                               target_guid: int) -> bool:
        group = self.group_for(session.player_guid)
        if group is None or group.loot_method != LOOT_METHOD_MASTER_LOOT:
            return False
        if int(group.master_looter_guid) != int(session.player_guid):
            return False
        if not group.is_member(target_guid):
            return False
        target = self.session_for(target_guid)
        if target is None or not hasattr(target, "_award_rolled_loot"):
            return False
        low_guid = creature.creature_low_guid(int(loot_target))
        if int(slot) not in group.master_slots.get(low_guid, set()):
            return False
        entry, count = await target._loot_slot_entry(loot_target, slot)
        if not entry:
            return False
        awarded = bool(await target._award_rolled_loot(
            loot_target, low_guid, entry, count, slot))
        if awarded:
            group.master_slots.get(low_guid, set()).discard(int(slot))
        return awarded

    # -- raid target icons ---------------------------------------------
    async def set_target_icon(self, session, icon_id: int,
                              target_guid: int) -> None:
        group = self.group_for(session.player_guid)
        if group is None or int(icon_id) >= 8:
            return
        if group.raid and not group.can_manage(session.player_guid):
            return
        target_guid = int(target_guid or 0)
        cleared = []
        if target_guid:
            for index, existing in enumerate(group.target_icons):
                if int(existing) == target_guid:
                    group.target_icons[index] = 0
                    cleared.append(index)
        group.target_icons[int(icon_id)] = target_guid
        await self._persist(group)
        sessions = self.member_sessions(group)
        for index in cleared:
            payload = packets.build_raid_target_update_set(
                int(session.player_guid), index, 0)
            for target in sessions:
                await target.send_packet(
                    Opcodes.MSG_RAID_TARGET_UPDATE, payload)
        payload = packets.build_raid_target_update_set(
            int(session.player_guid), int(icon_id), target_guid)
        for target in sessions:
            await target.send_packet(
                Opcodes.MSG_RAID_TARGET_UPDATE, payload)

    # -- ready check ---------------------------------------------------
    async def start_ready_check(self, session) -> None:
        group = self.group_for(session.player_guid)
        if group is None or not group.can_manage(session.player_guid):
            return
        group.ready_check_deadline = time.monotonic() + READY_CHECK_TIMEOUT
        payload = packets.build_ready_check(int(session.player_guid))
        for target in self.member_sessions(group):
            await target.send_packet(Opcodes.MSG_RAID_READY_CHECK, payload)

    async def confirm_ready(self, session, state: int) -> None:
        group = self.group_for(session.player_guid)
        if group is None:
            return
        payload = packets.build_ready_check_confirm(
            int(session.player_guid), int(state) & 0xFF)
        for target in self.member_sessions(group):
            if group.is_leader(target.player_guid) or group.is_assistant(
                    target.player_guid):
                await target.send_packet(
                    Opcodes.MSG_RAID_READY_CHECK_CONFIRM, payload)

    async def finish_ready_check(self, session) -> None:
        group = self.group_for(session.player_guid)
        if group is None or not group.can_manage(session.player_guid):
            return
        group.ready_check_deadline = 0.0
        for target in self.member_sessions(group):
            await target.send_packet(
                Opcodes.MSG_RAID_READY_CHECK_FINISHED, b"")

    async def tick_ready_checks(self, now: float) -> None:
        for group in list(self.groups.values()):
            if not group.ready_check_deadline:
                continue
            if group.ready_check_deadline > now:
                continue
            group.ready_check_deadline = 0.0
            for target in self.member_sessions(group):
                await target.send_packet(
                    Opcodes.MSG_RAID_READY_CHECK_FINISHED, b"")

    async def expire_offline_groups(self, now: Optional[int] = None) -> None:
        """Disband groups whose reconnect window elapsed with nobody online."""
        if self.offline_expiry <= 0:
            return
        now = int(now if now is not None else time.time())
        if now - int(self._last_offline_scan or 0) \
                < OFFLINE_GROUP_SCAN_INTERVAL:
            return
        self._last_offline_scan = now
        for group in list(self.groups.values()):
            if not group.offline_since:
                continue
            if self.member_sessions(group):
                group.offline_since = 0
                await self._persist(group)
                continue
            if now - int(group.offline_since) >= self.offline_expiry:
                await self.disband(group, notify=False)

    # -- session end ---------------------------------------------------
    async def on_session_end(self, player_guid: int, session=None) -> None:
        """Keep the member in the group; only the session goes away."""
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
        group = self.group_for(guid)
        if group is None:
            return
        session = session or self.session_for(guid)
        if session is not None:
            # A crashed socket never ran the logout handler; make sure the
            # member is reported offline even before the session is removed.
            session.in_world = False
        if group.size <= 1:
            # Nothing to preserve: a one-person group would just reappear as
            # an empty party frame after the next login.
            await self.disband(group, notify=False)
            return
        self._drop_roll_votes(group, guid)
        if self.member_sessions(group):
            group.offline_since = 0
        elif not group.offline_since:
            # Start the reconnect window when the last online member leaves.
            group.offline_since = int(time.time())
        await self._persist(group)
        await self.send_group_list(group)
        await self.send_member_stats(group)

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
