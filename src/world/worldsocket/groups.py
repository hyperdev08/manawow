"""Party/raid handlers and group combat helpers."""
from __future__ import annotations

import struct

from src.protocol.opcodes import Opcodes
from src.world.group_packets import (
    ERR_PARTY_RESULT_OK,
    build_party_member_stats_full,
)


class GroupsMixin:
    def _group_manager(self):
        server = getattr(self, "server", None)
        return getattr(server, "groups", None) if server else None

    def _group_member_sessions(self, map_id=None, exclude_self: bool = False):
        manager = self._group_manager()
        if manager is None or not self.player_guid:
            return []
        sessions = manager.group_member_sessions(
            int(self.player_guid),
            int((self.character or {}).get("map", 0))
            if map_id is None else int(map_id))
        if exclude_self:
            sessions = [session for session in sessions
                        if session is not self]
        return sessions

    def _is_group_member_with(self, other_guid: int) -> bool:
        manager = self._group_manager()
        return bool(manager is not None and manager.same_group(
            int(self.player_guid or 0), int(other_guid or 0)))

    async def _party_stats_tick(self, now: float) -> None:
        manager = self._group_manager()
        if manager is None or not self.player_guid:
            return
        if now - float(getattr(self, "_last_party_stats", 0.0)) < 1.0:
            return
        group = manager.group_for(int(self.player_guid))
        if group is None:
            return
        self._last_party_stats = now
        payload = build_party_member_stats_full(self)
        for session in manager.member_sessions(group):
            if session is not self:
                await session.send_packet(
                    Opcodes.SMSG_PARTY_MEMBER_STATS_FULL, payload)

    # -- CMSG handlers -------------------------------------------------
    async def handle_group_invite(self, data: bytes):
        manager = self._group_manager()
        if manager is None or not self.character or len(data) < 1:
            return
        name, _pos = self._read_cstring(data, 0)
        await manager.invite(self, name)

    async def handle_group_accept(self, data: bytes):
        manager = self._group_manager()
        if manager is not None:
            await manager.accept(self)

    async def handle_group_decline(self, data: bytes):
        manager = self._group_manager()
        if manager is not None:
            await manager.decline(self)

    async def handle_group_uninvite(self, data: bytes):
        manager = self._group_manager()
        if manager is None or len(data) < 1:
            return
        name, _pos = self._read_cstring(data, 0)
        await manager.uninvite(self, target_name=name)

    async def handle_group_uninvite_guid(self, data: bytes):
        manager = self._group_manager()
        if manager is None or len(data) < 8:
            return
        guid = struct.unpack_from("<Q", data, 0)[0]
        await manager.uninvite(self, target_guid=int(guid))

    async def handle_group_set_leader(self, data: bytes):
        manager = self._group_manager()
        if manager is None or len(data) < 8:
            return
        guid = struct.unpack_from("<Q", data, 0)[0]
        await manager.set_leader(self, int(guid))

    async def handle_group_disband(self, data: bytes):
        manager = self._group_manager()
        if manager is not None and self.player_guid:
            if manager.group_for(int(self.player_guid)) is not None:
                await manager.send_party_result(
                    self, 1, str(self.character.get("name", "")),
                    ERR_PARTY_RESULT_OK)
            await manager.leave(int(self.player_guid))

    async def handle_group_raid_convert(self, data: bytes):
        manager = self._group_manager()
        if manager is not None:
            await manager.convert_raid(self)

    async def handle_group_change_subgroup(self, data: bytes):
        manager = self._group_manager()
        if manager is None or len(data) < 2:
            return
        name, pos = self._read_cstring(data, 0)
        subgroup = data[pos] if pos < len(data) else 0
        await manager.change_subgroup(self, name, subgroup)

    async def handle_group_swap_subgroup(self, data: bytes):
        manager = self._group_manager()
        if manager is None or not self.player_guid or len(data) < 2:
            return
        first, pos = self._read_cstring(data, 0)
        second, _pos = self._read_cstring(data, pos)
        group = manager.group_for(int(self.player_guid))
        if group is None or not group.is_leader(self.player_guid):
            return
        first_session = manager.session_by_name(first)
        second_session = manager.session_by_name(second)
        if first_session is None or second_session is None:
            return
        first_slot = group.members.get(int(first_session.player_guid))
        second_slot = group.members.get(int(second_session.player_guid))
        if first_slot is None or second_slot is None:
            return
        first_slot.subgroup, second_slot.subgroup = (
            second_slot.subgroup, first_slot.subgroup)
        await manager.send_group_list(group)

    async def handle_request_party_member_stats(self, data: bytes):
        manager = self._group_manager()
        if manager is None or len(data) < 8:
            return
        guid = struct.unpack_from("<Q", data, 0)[0]
        await manager.request_member_stats(self, int(guid))
