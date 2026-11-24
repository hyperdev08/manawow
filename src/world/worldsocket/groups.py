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
        self._last_party_stats = now
        group = manager.group_for(int(self.player_guid))
        if group is not None:
            payload = build_party_member_stats_full(self)
            for session in manager.member_sessions(group):
                if session is not self:
                    await session.send_packet(
                        Opcodes.SMSG_PARTY_MEMBER_STATS_FULL, payload)
            await manager.tick_rolls(now)
            await manager.tick_ready_checks(now)
        # Runs even for ungrouped players so a fully offline group can expire
        # while the rest of the realm is still online.
        await manager.expire_offline_groups()

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
        await manager.swap_subgroup(self, first, second)

    async def handle_group_assistant_leader(self, data: bytes):
        manager = self._group_manager()
        if manager is None or len(data) < 9:
            return
        guid = struct.unpack_from("<Q", data, 0)[0]
        apply = bool(data[8])
        await manager.set_assistant(self, int(guid), apply)

    async def handle_loot_method(self, data: bytes):
        manager = self._group_manager()
        if manager is None or len(data) < 16:
            return
        method = struct.unpack_from("<I", data, 0)[0]
        master = struct.unpack_from("<Q", data, 4)[0]
        threshold = struct.unpack_from("<I", data, 12)[0]
        await manager.set_loot_method(
            self, int(method), int(master), int(threshold))

    async def handle_loot_roll(self, data: bytes):
        manager = self._group_manager()
        if manager is None or len(data) < 13:
            return
        item_guid = struct.unpack_from("<Q", data, 0)[0]
        slot = struct.unpack_from("<I", data, 8)[0]
        roll_type = data[12]
        await manager.cast_roll_vote(
            self, int(item_guid), int(slot), int(roll_type))

    async def handle_opt_out_of_loot(self, data: bytes):
        manager = self._group_manager()
        if manager is None or len(data) < 4:
            return
        enabled = struct.unpack_from("<I", data, 0)[0]
        await manager.set_pass_on_loot(self, bool(enabled))

    async def handle_raid_target_update(self, data: bytes):
        manager = self._group_manager()
        if manager is None or not self.player_guid or not data:
            return
        icon_id = data[0]
        if icon_id == 0xFF:
            group = manager.group_for(int(self.player_guid))
            if group is not None:
                await manager.send_target_icons(self, group)
            return
        if len(data) < 10:
            return
        target_guid = struct.unpack_from("<Q", data, 1)[0]
        await manager.set_target_icon(self, int(icon_id), int(target_guid))

    async def handle_raid_ready_check(self, data: bytes):
        manager = self._group_manager()
        if manager is None or not self.player_guid:
            return
        if not data:
            await manager.start_ready_check(self)
        else:
            await manager.confirm_ready(self, int(data[0]))

    async def handle_raid_ready_check_finished(self, data: bytes):
        manager = self._group_manager()
        if manager is not None and self.player_guid:
            await manager.finish_ready_check(self)

    async def handle_request_party_member_stats(self, data: bytes):
        manager = self._group_manager()
        if manager is None or len(data) < 8:
            return
        guid = struct.unpack_from("<Q", data, 0)[0]
        await manager.request_member_stats(self, int(guid))
