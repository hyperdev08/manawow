"""3.3.5a group/party packet builders."""
from __future__ import annotations

import struct

from src.world.movement import pack_guid

GROUP_FLAG_RAID = 0x02

MEMBER_STATUS_OFFLINE = 0x0000
MEMBER_STATUS_ONLINE = 0x0001
MEMBER_STATUS_DEAD = 0x0004
MEMBER_STATUS_GHOST = 0x0008

MEMBER_FLAG_ASSISTANT = 0x01

LOOT_METHOD_FREE_FOR_ALL = 0
LOOT_METHOD_ROUND_ROBIN = 1
LOOT_METHOD_MASTER_LOOT = 2
LOOT_METHOD_GROUP_LOOT = 3
LOOT_METHOD_NEED_BEFORE_GREED = 4

ROLL_PASS = 0
ROLL_NEED = 1
ROLL_GREED = 2
ROLL_DISENCHANT = 3

ROLL_FLAG_TYPE_PASS = 0x01
ROLL_FLAG_TYPE_NEED = 0x02
ROLL_FLAG_TYPE_GREED = 0x04
ROLL_FLAG_TYPE_DISENCHANT = 0x08
ROLL_FLAG_TYPE_MASK = 0x0F

LOOT_SLOT_TYPE_ALLOW_LOOT = 0
LOOT_SLOT_TYPE_ROLL_ONGOING = 1
LOOT_SLOT_TYPE_MASTER = 2
LOOT_SLOT_TYPE_LOCKED = 3
LOOT_SLOT_TYPE_OWNER = 4

ERR_PARTY_RESULT_OK = 0
ERR_BAD_PLAYER_NAME_S = 1
ERR_TARGET_NOT_IN_GROUP_S = 2
ERR_GROUP_FULL = 4
ERR_ALREADY_IN_GROUP_S = 5
ERR_NOT_IN_GROUP = 6
ERR_NOT_LEADER = 7
ERR_PLAYER_WRONG_FACTION = 8
ERR_IGNORE = 9


def _cstring(value: str) -> bytes:
    return str(value).encode("utf-8", errors="replace") + b"\x00"


def member_status(session) -> int:
    if session is None or not getattr(session, "in_world", False):
        return MEMBER_STATUS_OFFLINE
    status = MEMBER_STATUS_ONLINE
    try:
        combat = session._get_combat_state()
        if combat.get("dead"):
            status |= MEMBER_STATUS_DEAD
        if combat.get("ghost"):
            status |= MEMBER_STATUS_GHOST
    except Exception:
        pass
    return status


def build_group_list(group, viewer_guid: int) -> bytes:
    members = list(group.members.values())
    viewer = group.members.get(int(viewer_guid))
    if viewer is None:
        return build_group_list_empty()
    out = bytearray()
    out += struct.pack("<BBBB",
                       GROUP_FLAG_RAID if group.raid else 0,
                       int(viewer.subgroup) & 0xFF,
                       int(getattr(viewer, "member_flags", 0)) & 0xFF,
                       int(getattr(viewer, "roles", 0)) & 0xFF)
    out += struct.pack("<Q", int(group.guid))
    out += struct.pack("<I", group.next_counter())
    out += struct.pack("<I", max(0, len(members) - 1))
    for member in members:
        if int(member.guid) == int(viewer_guid):
            continue
        session = group.manager.session_for(member.guid)
        out += _cstring(member.name)
        out += struct.pack("<Q", int(member.guid))
        out += struct.pack("<BBBB", member_status(session),
                           int(member.subgroup) & 0xFF,
                           int(getattr(member, "member_flags", 0)) & 0xFF,
                           int(getattr(member, "roles", 0)) & 0xFF)
    out += struct.pack("<Q", int(group.leader_guid))
    if len(members) > 1:
        loot_method = int(getattr(group, "loot_method",
                                   LOOT_METHOD_FREE_FOR_ALL))
        out += struct.pack("<B", loot_method)
        out += struct.pack(
            "<Q", int(getattr(group, "master_looter_guid", 0) or 0)
            if loot_method == LOOT_METHOD_MASTER_LOOT else 0)
        out += struct.pack("<BBB",
                           int(getattr(group, "loot_threshold", 2) or 2),
                           0, 0)                   # dungeon, raid difficulty
        out += struct.pack("<B", 0)                 # dynamic heroic
    return bytes(out)


def build_group_list_empty() -> bytes:
    return (struct.pack("<BBBB", 0x10, 0, 0, 0)
            + struct.pack("<QII", 0, 0, 0)
            + struct.pack("<Q", 0))


def build_group_invite(inviter_name: str) -> bytes:
    return (struct.pack("<B", 1) + _cstring(inviter_name)
            + struct.pack("<I", 0) + struct.pack("<B", 0)
            + struct.pack("<I", 0))


def build_group_decline(name: str) -> bytes:
    return _cstring(name)


def build_group_uninvite(name: str) -> bytes:
    return _cstring(name)


def build_group_set_leader(name: str) -> bytes:
    return _cstring(name)


def build_party_command_result(operation: int, name: str,
                               result: int) -> bytes:
    return (struct.pack("<I", int(operation)) + _cstring(name)
            + struct.pack("<II", int(result), 0))


def build_party_member_stats_full(session) -> bytes:
    character = session.character or {}
    status = member_status(session)
    health = int(character.get("health", 0) or 0)
    max_health = int(character.get("max_health", 0) or 0)
    power_type = int(session._power_type()) if hasattr(
        session, "_power_type") else 0
    power = int(character.get("power", 0) or 0)
    max_power = int(character.get("max_power", 0) or 0)
    level = int(character.get("level", 1) or 1)
    zone = int(character.get("zone", 0) or 0)
    x = int(float(character.get("x", 0.0) or 0.0))
    y = int(float(character.get("y", 0.0) or 0.0))
    mask = 0x00040BFF
    out = bytearray()
    out += struct.pack("<B", 0)                 # arena opponent
    out += pack_guid(int(session.player_guid or 0))
    out += struct.pack("<I", mask)
    out += struct.pack("<H", status)
    out += struct.pack("<I", health)
    out += struct.pack("<I", max_health)
    out += struct.pack("<B", power_type)
    out += struct.pack("<H", max(0, min(0xFFFF, power)))
    out += struct.pack("<H", max(0, min(0xFFFF, max_power)))
    out += struct.pack("<H", level)
    out += struct.pack("<H", zone)
    out += struct.pack("<hh", max(-32768, min(32767, x)),
                       max(-32768, min(32767, y)))
    out += struct.pack("<Q", 0)                 # aura mask
    out += struct.pack("<B", 0)                 # pet name (none)
    out += struct.pack("<Q", 0)                 # pet aura mask
    return bytes(out)


# ---------------------------------------------------------------------------
# Loot rolls / master loot (TrinityCore 3.3.5 Group::SendLoot*)
# ---------------------------------------------------------------------------
def build_loot_start_roll(item_guid: int, map_id: int, slot: int,
                          item_entry: int, random_suffix: int = 0,
                          random_property: int = 0, count: int = 1,
                          countdown: int = 60000,
                          vote_mask: int = ROLL_FLAG_TYPE_MASK) -> bytes:
    return struct.pack(
        "<QIIIIIIIB",
        int(item_guid) & 0xFFFFFFFFFFFFFFFF, int(map_id) & 0xFFFFFFFF,
        int(slot) & 0xFFFFFFFF, int(item_entry) & 0xFFFFFFFF,
        int(random_suffix) & 0xFFFFFFFF, int(random_property) & 0xFFFFFFFF,
        int(count) & 0xFFFFFFFF, int(countdown) & 0xFFFFFFFF,
        int(vote_mask) & 0xFF)


def build_loot_roll(source_guid: int, slot: int, target_guid: int,
                    item_entry: int, roll_number: int, roll_type: int,
                    random_suffix: int = 0, random_property: int = 0,
                    auto_pass: bool = False) -> bytes:
    return struct.pack(
        "<QIQIIIBBB",
        int(source_guid) & 0xFFFFFFFFFFFFFFFF, int(slot) & 0xFFFFFFFF,
        int(target_guid) & 0xFFFFFFFFFFFFFFFF,
        int(item_entry) & 0xFFFFFFFF, int(random_suffix) & 0xFFFFFFFF,
        int(random_property) & 0xFFFFFFFF, int(roll_number) & 0xFF,
        int(roll_type) & 0xFF, 1 if auto_pass else 0)


def build_loot_roll_won(source_guid: int, slot: int, item_entry: int,
                        target_guid: int, roll_number: int, roll_type: int,
                        random_suffix: int = 0,
                        random_property: int = 0) -> bytes:
    return struct.pack(
        "<QIIIIQBB",
        int(source_guid) & 0xFFFFFFFFFFFFFFFF, int(slot) & 0xFFFFFFFF,
        int(item_entry) & 0xFFFFFFFF, int(random_suffix) & 0xFFFFFFFF,
        int(random_property) & 0xFFFFFFFF,
        int(target_guid) & 0xFFFFFFFFFFFFFFFF,
        int(roll_number) & 0xFF, int(roll_type) & 0xFF)


def build_loot_all_passed(item_guid: int, slot: int, item_entry: int,
                          random_property: int = 0,
                          random_suffix: int = 0) -> bytes:
    return struct.pack(
        "<QIIII", int(item_guid) & 0xFFFFFFFFFFFFFFFF,
        int(slot) & 0xFFFFFFFF, int(item_entry) & 0xFFFFFFFF,
        int(random_property) & 0xFFFFFFFF,
        int(random_suffix) & 0xFFFFFFFF)


def build_loot_master_list(guids) -> bytes:
    values = [int(guid) & 0xFFFFFFFFFFFFFFFF for guid in guids]
    return struct.pack("<B", len(values)) + b"".join(
        struct.pack("<Q", guid) for guid in values)


def build_raid_target_update_set(who_guid: int, icon_id: int,
                                 target_guid: int) -> bytes:
    return (struct.pack("<B", 0)
            + struct.pack("<Q", int(who_guid) & 0xFFFFFFFFFFFFFFFF)
            + struct.pack("<B", int(icon_id) & 0xFF)
            + struct.pack("<Q", int(target_guid) & 0xFFFFFFFFFFFFFFFF))


def build_raid_target_update_list(icons) -> bytes:
    out = bytearray(struct.pack("<B", 1))
    for index, target in enumerate(icons or []):
        if not int(target or 0):
            continue
        out += struct.pack("<B", index)
        out += struct.pack("<Q", int(target) & 0xFFFFFFFFFFFFFFFF)
    return bytes(out)


def build_ready_check(initiator_guid: int) -> bytes:
    return struct.pack("<Q", int(initiator_guid) & 0xFFFFFFFFFFFFFFFF)


def build_ready_check_confirm(guid: int, state: int) -> bytes:
    return (struct.pack("<Q", int(guid) & 0xFFFFFFFFFFFFFFFF)
            + struct.pack("<B", int(state) & 0xFF))


def build_ready_check_finished() -> bytes:
    return b""
