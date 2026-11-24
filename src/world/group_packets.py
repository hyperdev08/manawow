"""3.3.5a group/party packet builders."""
from __future__ import annotations

import struct

from src.world.movement import pack_guid

GROUP_FLAG_RAID = 0x02

MEMBER_STATUS_ONLINE = 0x0001
MEMBER_STATUS_DEAD = 0x0004
MEMBER_STATUS_GHOST = 0x0008
MEMBER_STATUS_ZONE_OUT = 0x0020

LOOT_METHOD_FREE_FOR_ALL = 0

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
    status = 0
    if session is None or not getattr(session, "in_world", False):
        return MEMBER_STATUS_ZONE_OUT
    status |= MEMBER_STATUS_ONLINE
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
                       0,          # member flags (assistant)
                       0)          # LFG roles
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
                           int(member.subgroup) & 0xFF, 0, 0)
    out += struct.pack("<Q", int(group.leader_guid))
    if len(members) > 1:
        out += struct.pack("<B", LOOT_METHOD_FREE_FOR_ALL)
        out += struct.pack("<Q", 0)      # master looter
        out += struct.pack("<BBB", 2, 0, 0)   # threshold, dungeon, raid
        out += struct.pack("<B", 0)      # dynamic heroic
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
