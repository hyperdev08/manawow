"""Pet action bar, commands, autocast toggles and rename support.

``SMSG_PET_SPELLS`` is the 3.3.5 packet that creates the client's pet action
bar.  Its layout is::

    uint64 pet guid
    uint16 creature family
    uint32 duration (0 = permanent)
    uint8  react state      (0 passive, 1 defensive, 2 aggressive)
    uint8  command state    (0 stay, 1 follow)
    uint16 flags            (always 0 for normal pets)
    uint32 action_bar[10]   (packed action | type << 24)
    uint8  spell_count
    uint32 spells[spell_count]
    uint8  cooldown_count
    ...

Packed action buttons use the same encoding as the player action bar: the low
24 bits are a spell/command id and the high byte is the action type.  Pets use
``ACT_COMMAND``/``ACT_REACTION`` for the fixed buttons and
``ACT_PASSIVE``/``ACT_DISABLED``/``ACT_ENABLED`` for spells.
"""
from __future__ import annotations

import re
import sqlite3
import struct
import time

from src.protocol.opcodes import Opcodes
from src.world import creature
from src.world import spells
from src.world import update_fields as F


ACT_PASSIVE = 0x01
ACT_DISABLED = 0x81
ACT_ENABLED = 0xC1
ACT_COMMAND = 0x07
ACT_REACTION = 0x06

COMMAND_STAY = 0
COMMAND_FOLLOW = 1
COMMAND_ATTACK = 2
COMMAND_ABANDON = 3

REACT_PASSIVE = 0
REACT_DEFENSIVE = 1
REACT_AGGRESSIVE = 2

PET_ACTION_SLOTS = 10
PET_SPELL_SLOT_START = 3
PET_SPELL_SLOT_END = 7

UNIT_PET_FLAG_CAN_BE_RENAMED = 0x01
UNIT_PET_FLAG_CAN_BE_ABANDONED = 0x02

_PET_NAME_RE = re.compile(r"^[A-Za-z]{2,12}$")


def pack_action(action: int, kind: int) -> int:
    return (int(action) & 0x00FFFFFF) | ((int(kind) & 0xFF) << 24)


def unpack_action(packed: int) -> tuple[int, int]:
    packed = int(packed or 0) & 0xFFFFFFFF
    return packed & 0x00FFFFFF, (packed >> 24) & 0xFF


def _read_cstring(data: bytes, offset: int) -> tuple[str, int]:
    end = data.find(b"\x00", offset)
    if end < 0:
        return data[offset:].decode("utf-8", "replace"), len(data)
    return data[offset:end].decode("utf-8", "replace"), end + 1


class PetsMixin:
    # ------------------------------------------------------------------
    # State / lookup
    # ------------------------------------------------------------------
    def _pet_spell_ids(self, spawn) -> list[int]:
        """Spells a summoned pet knows from the creature template tables."""
        path = self.config.database.world_path
        entry = int(spawn["entry"])
        ids: list[int] = []
        try:
            ids.extend(int(value) for value in spells.get_creature_spells(
                path, entry) if value)
            list_id = int(spawn["spell_list_id"]
                          if "spell_list_id" in spawn.keys() else 0)
            if list_id:
                ids.extend(int(row["spell_id"])
                           for row in spells.get_creature_spell_list(
                               path, list_id) if row.get("spell_id"))
        except (sqlite3.Error, OSError):
            return []
        valid = []
        for spell_id in dict.fromkeys(ids):
            try:
                if spells.get_spell_template(path, int(spell_id)) is not None:
                    valid.append(int(spell_id))
            except (sqlite3.Error, OSError):
                continue
        return valid

    def _pet_autocastable(self, spell_id: int) -> bool:
        try:
            template = spells.get_spell_template(
                self.config.database.world_path, int(spell_id))
        except (sqlite3.Error, OSError):
            return False
        if template is None or template.is_passive():
            return False
        if int(template.attributes_ex) & spells.SPELL_ATTR_EX_NO_AUTOCAST_AI:
            return False
        return True

    def _pet_for_guid(self, guid: int):
        low = creature.creature_low_guid(int(guid))
        spawn = self._summon_spawns().get(int(low))
        if spawn is None:
            return None
        state = self._summon_state(int(low))
        if not state.get("is_pet"):
            return None
        return int(low), spawn, state

    def _active_pet(self):
        for low, spawn in self._summon_spawns().items():
            state = self._summon_state(low)
            if state.get("is_pet") and not state.get("stationary"):
                return int(low), spawn, state
        return None

    def _pet_duration_ms(self, state) -> int:
        expires = state.get("expires")
        if expires is None:
            return 0
        return max(0, int((float(expires) - time.monotonic()) * 1000))

    # ------------------------------------------------------------------
    # Action bar construction
    # ------------------------------------------------------------------
    def _pet_build_default_bar(self, state: dict) -> list[int]:
        states = state["pet_spell_states"]
        bar = [
            pack_action(COMMAND_ATTACK, ACT_COMMAND),
            pack_action(COMMAND_FOLLOW, ACT_COMMAND),
            pack_action(COMMAND_STAY, ACT_COMMAND),
        ]
        bar.extend(pack_action(0, ACT_PASSIVE)
                   for _ in range(PET_SPELL_SLOT_START, PET_SPELL_SLOT_END))
        bar.extend((
            pack_action(REACT_AGGRESSIVE, ACT_REACTION),
            pack_action(REACT_DEFENSIVE, ACT_REACTION),
            pack_action(REACT_PASSIVE, ACT_REACTION),
        ))
        spells_in_bar = [spell_id for spell_id in state["pet_spells"]
                         if states.get(spell_id) != ACT_PASSIVE]
        for index, spell_id in enumerate(
                spells_in_bar[:PET_SPELL_SLOT_END - PET_SPELL_SLOT_START]):
            bar[PET_SPELL_SLOT_START + index] = pack_action(
                spell_id, states[spell_id])
        return bar[:PET_ACTION_SLOTS]

    async def _pet_initialize(self, low_guid: int, spawn, state: dict) -> None:
        """Create the default pet bar and send it after the pet is visible."""
        state.setdefault("react_state", REACT_DEFENSIVE)
        state.setdefault("command_state", COMMAND_FOLLOW)
        state.setdefault("forced_target", 0)
        state.setdefault("stay_position", None)
        state["pet_number"] = int(spawn["pet_number"]
                                  if "pet_number" in spawn.keys() else low_guid)
        spell_ids = self._pet_spell_ids(spawn)
        states: dict[int, int] = {}
        for spell_id in spell_ids:
            states[spell_id] = (ACT_PASSIVE
                                if not self._pet_autocastable(spell_id)
                                else ACT_DISABLED)
        state["pet_spells"] = spell_ids
        state["pet_spell_states"] = states
        state["pet_autocast"] = set()
        state["pet_action_bar"] = self._pet_build_default_bar(state)
        await self._send_pet_spells(low_guid)

    def _pet_spells_payload(self, spawn, state: dict) -> bytes:
        bar = list(state.get("pet_action_bar") or ())
        bar.extend(0 for _ in range(PET_ACTION_SLOTS - len(bar)))
        out = bytearray(struct.pack(
            "<QHIBBH",
            creature.creature_guid(int(spawn["guid"]), int(spawn["entry"])),
            int(spawn.get("family", 0) or 0) & 0xFFFF,
            self._pet_duration_ms(state),
            int(state.get("react_state", REACT_DEFENSIVE)) & 0xFF,
            int(state.get("command_state", COMMAND_FOLLOW)) & 0xFF,
            0,
        ))
        for packed in bar[:PET_ACTION_SLOTS]:
            out += struct.pack("<I", int(packed) & 0xFFFFFFFF)
        spell_ids = list(state.get("pet_spells") or ())
        out += struct.pack("<B", len(spell_ids) & 0xFF)
        spell_states = state.get("pet_spell_states") or {}
        for spell_id in spell_ids:
            out += struct.pack(
                "<I", pack_action(spell_id,
                                  spell_states.get(spell_id, ACT_PASSIVE)))
        out += b"\x00"                      # no active cooldowns
        return bytes(out)

    async def _send_pet_spells(self, low_guid: int | None = None) -> None:
        if low_guid is None:
            active = self._active_pet()
            if active is None:
                return
            low_guid, spawn, state = active
        else:
            spawn = self._summon_spawns().get(int(low_guid))
            if spawn is None:
                return
            state = self._summon_state(int(low_guid))
            if not state.get("is_pet"):
                return
        await self.send_packet(
            Opcodes.SMSG_PET_SPELLS,
            self._pet_spells_payload(spawn, state))

    # ------------------------------------------------------------------
    # Autocast / action-bar edits
    # ------------------------------------------------------------------
    def _pet_set_spell_state(self, state: dict, spell_id: int,
                             active: int) -> None:
        states = state.setdefault("pet_spell_states", {})
        if int(spell_id) not in states:
            return
        states[int(spell_id)] = int(active)
        autocast = state.setdefault("pet_autocast", set())
        if active == ACT_ENABLED:
            autocast.add(int(spell_id))
        else:
            autocast.discard(int(spell_id))
        bar = state.setdefault("pet_action_bar", [])
        for index, packed in enumerate(bar):
            action, _kind = unpack_action(packed)
            if action == int(spell_id):
                bar[index] = pack_action(spell_id, active)

    async def _pet_toggle_autocast(self, low_guid: int, spawn, state: dict,
                                   spell_id: int, enabled: bool) -> None:
        if int(spell_id) not in state.get("pet_spell_states", {}):
            return
        if not self._pet_autocastable(int(spell_id)):
            return
        self._pet_set_spell_state(
            state, int(spell_id), ACT_ENABLED if enabled else ACT_DISABLED)
        await self._send_pet_spells(low_guid)

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------
    async def _pet_command_attack(self, low_guid: int, spawn,
                                  state: dict, target_guid: int) -> None:
        low_target = creature.creature_low_guid(int(target_guid))
        if low_target in self._summon_spawns():
            return
        target = self._get_creature_spawn(int(low_target))
        if target is None or int(target["dead_since"] or 0) > 0:
            return
        state["forced_target"] = int(target_guid)
        state["command_state"] = COMMAND_FOLLOW
        state["stay_position"] = None
        state["moving_until"] = 0.0

    def _pet_command_follow(self, spawn, state: dict) -> None:
        state["forced_target"] = 0
        state["target"] = 0
        state["command_state"] = COMMAND_FOLLOW
        state["stay_position"] = None

    def _pet_command_stay(self, spawn, state: dict) -> None:
        state["forced_target"] = 0
        state["target"] = 0
        state["command_state"] = COMMAND_STAY
        state["stay_position"] = (float(spawn["x"]), float(spawn["y"]),
                                  float(spawn["z"]))
        state["moving_until"] = 0.0

    async def _pet_command_reaction(self, low_guid: int, state: dict,
                                    reaction: int) -> None:
        if reaction not in (REACT_PASSIVE, REACT_DEFENSIVE,
                            REACT_AGGRESSIVE):
            return
        state["react_state"] = int(reaction)
        if reaction == REACT_PASSIVE:
            state["forced_target"] = 0
            state["target"] = 0

    # ------------------------------------------------------------------
    # Client opcodes
    # ------------------------------------------------------------------
    async def handle_pet_action(self, data: bytes) -> None:
        if not self.in_world or not self.character or len(data) < 20:
            return
        guid, packed, target = struct.unpack_from("<QIQ", data, 0)
        pet = self._pet_for_guid(guid)
        if pet is None:
            return
        low_guid, spawn, state = pet
        action, kind = unpack_action(packed)
        if kind == ACT_COMMAND:
            if action == COMMAND_ATTACK:
                await self._pet_command_attack(
                    low_guid, spawn, state, target)
            elif action == COMMAND_FOLLOW:
                self._pet_command_follow(spawn, state)
            elif action == COMMAND_STAY:
                self._pet_command_stay(spawn, state)
            elif action == COMMAND_ABANDON:
                await self._dismiss_summon(low_guid)
            return
        if kind == ACT_REACTION:
            await self._pet_command_reaction(low_guid, state, action)
            return
        # Pet spell buttons are cast actions.  Pet spell casting is not part of
        # the summon AI yet, but accepting the button here keeps the client
        # from feeling unresponsive while the action bar is usable.
        if kind in (ACT_ENABLED, ACT_DISABLED, ACT_PASSIVE):
            self.log.debug(
                f"Pet {low_guid} spell button {action} (type 0x{kind:02X})")

    async def handle_pet_set_action(self, data: bytes) -> None:
        if not self.in_world or not self.character or len(data) < 16:
            return
        guid = struct.unpack_from("<Q", data, 0)[0]
        pet = self._pet_for_guid(guid)
        if pet is None:
            return
        low_guid, _spawn, state = pet
        count = 2 if len(data) >= 24 else 1
        bar = state.setdefault("pet_action_bar", [])
        for index in range(count):
            position, packed = struct.unpack_from("<II", data, 8 + index * 8)
            if position >= PET_ACTION_SLOTS:
                continue
            action, kind = unpack_action(packed)
            # Command/reaction buttons can be moved but not removed by a
            # one-slot edit packet.
            if count == 1 and kind in (ACT_COMMAND, ACT_REACTION):
                continue
            if len(bar) < PET_ACTION_SLOTS:
                bar.extend(0 for _ in range(PET_ACTION_SLOTS - len(bar)))
            bar[int(position)] = int(packed) & 0xFFFFFFFF
            if (kind in (ACT_ENABLED, ACT_DISABLED)
                    and action in state.get("pet_spell_states", {})):
                if kind == ACT_ENABLED and not self._pet_autocastable(action):
                    bar[int(position)] = pack_action(action, ACT_PASSIVE)
                    self._pet_set_spell_state(state, action, ACT_PASSIVE)
                else:
                    self._pet_set_spell_state(state, action, kind)
        await self._send_pet_spells(low_guid)

    async def handle_pet_spell_autocast(self, data: bytes) -> None:
        if not self.in_world or not self.character or len(data) < 13:
            return
        guid, spell_id, enabled = struct.unpack_from("<QIB", data, 0)
        pet = self._pet_for_guid(guid)
        if pet is None:
            return
        low_guid, spawn, state = pet
        await self._pet_toggle_autocast(
            low_guid, spawn, state, int(spell_id), bool(enabled))

    async def handle_pet_abandon(self, data: bytes) -> None:
        if not self.in_world or not self.character or len(data) < 8:
            return
        guid = struct.unpack_from("<Q", data, 0)[0]
        pet = self._pet_for_guid(guid)
        if pet is None:
            return
        await self._dismiss_summon(pet[0])

    async def handle_pet_stop_attack(self, data: bytes) -> None:
        if not self.in_world or not self.character or len(data) < 8:
            return
        guid = struct.unpack_from("<Q", data, 0)[0]
        pet = self._pet_for_guid(guid)
        if pet is None:
            return
        _low, _spawn, state = pet
        state["forced_target"] = 0
        state["target"] = 0

    async def handle_request_pet_info(self, data: bytes) -> None:
        await self._send_pet_spells()

    # ------------------------------------------------------------------
    # Name query / rename
    # ------------------------------------------------------------------
    async def _send_pet_name_query_response(self, pet_number: int, name: str,
                                            timestamp: int) -> None:
        payload = (struct.pack("<I", int(pet_number) & 0xFFFFFFFF)
                   + str(name).encode("utf-8", "replace") + b"\x00"
                   + struct.pack("<I", int(timestamp) & 0xFFFFFFFF)
                   + b"\x00")
        await self.send_packet(Opcodes.SMSG_PET_NAME_QUERY_RESPONSE, payload)

    async def _send_pet_name_invalid(self, name: str, reason: int = 1) -> None:
        payload = (struct.pack("<I", int(reason))
                   + str(name).encode("utf-8", "replace") + b"\x00" + b"\x00")
        await self.send_packet(Opcodes.SMSG_PET_NAME_INVALID, payload)

    async def handle_pet_name_query(self, data: bytes) -> None:
        if len(data) < 12:
            return
        pet_number, guid = struct.unpack_from("<IQ", data, 0)
        pet = self._pet_for_guid(guid)
        if pet is None:
            await self.send_packet(
                Opcodes.SMSG_PET_NAME_QUERY_RESPONSE,
                struct.pack("<IBIB", int(pet_number), 0, 0, 0))
            return
        _low, spawn, state = pet
        await self._send_pet_name_query_response(
            pet_number, str(spawn.get("name", "")),
            int(spawn.get("pet_name_timestamp", 0) or 0))

    async def handle_pet_rename(self, data: bytes) -> None:
        if not self.in_world or not self.character or len(data) < 9:
            return
        guid = struct.unpack_from("<Q", data, 0)[0]
        name, _offset = _read_cstring(data, 8)
        pet = self._pet_for_guid(guid)
        if pet is None:
            return
        low_guid, spawn, state = pet
        flags = int(spawn.get("pet_flags", 0) or 0)
        if not (flags & UNIT_PET_FLAG_CAN_BE_RENAMED):
            await self._send_pet_name_invalid(name)
            return
        if not _PET_NAME_RE.match(name):
            await self._send_pet_name_invalid(name)
            return
        timestamp = int(time.time())
        spawn["name"] = name
        spawn["pet_name_timestamp"] = timestamp
        spawn["pet_flags"] = flags & ~UNIT_PET_FLAG_CAN_BE_RENAMED
        state["pet_name_timestamp"] = timestamp
        if flags & UNIT_PET_FLAG_CAN_BE_ABANDONED:
            spawn["pet_flags"] |= UNIT_PET_FLAG_CAN_BE_ABANDONED
        await self._send_pet_name_query_response(
            int(state.get("pet_number") or low_guid), name, timestamp)
        if hasattr(self, "_send_creature_fields"):
            await self._send_creature_fields(int(low_guid), {
                F.UNIT_FIELD_PET_NAME_TIMESTAMP: timestamp,
                F.UNIT_FIELD_BYTES_2: (
                    int(spawn["pet_flags"]) & 0xFF) << 16,
            })
