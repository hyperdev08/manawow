"""Loot generation, rolls, persistence and loot opcodes."""
from __future__ import annotations

import json
import math
import random
import sqlite3
import struct

from typing import Dict, List, Tuple

from src.common.db_io import execute_write, run_db_write
from src.protocol.opcodes import Opcodes
from src.world import character_db
from src.world import creature
from src.world import gameobject
from src.world import login_packets
from src.world import object_update
from src.world import update_fields as F

class LootMixin:
    INVENTORY_FULL = 50
    MAX_LOOT_ITEMS = 16

    async def handle_loot(self, data: bytes):
        """``CMSG_LOOT``: open a creature corpse's loot window."""
        if not self.in_world or not self.character:
            return
        if len(data) < 8:
            return
        target_guid = struct.unpack_from("<Q", data, 0)[0]
        combat = self._get_combat_state()
        if combat.get("dead"):
            return
        low_guid = creature.creature_low_guid(target_guid)
        spawn = self._get_creature_spawn(low_guid)
        if spawn is None:
            return
        if int(spawn["dead_since"] or 0) <= 0:
            # Not a corpse: the client still expects an (empty) answer, or its
            # loot button stays stuck in the "opening" state.
            await self._send_loot_response(target_guid, [], 0)
            return
        loot_owner = (int(spawn["loot_owner"] or 0)
                      if "loot_owner" in spawn.keys() else 0)
        manager = (self._group_manager()
                   if hasattr(self, "_group_manager") else None)
        if loot_owner and loot_owner != int(self.player_guid or 0):
            if manager is None or not manager.same_group(
                    loot_owner, int(self.player_guid or 0)):
                await self._send_loot_response(target_guid, [], 0)
                return
        if manager is not None and not manager.acquire_loot(
                low_guid, int(self.player_guid or 0)):
            # Another group member is already looting this corpse.
            await self._send_loot_response(target_guid, [], 0)
            return
        # Read the loot that was rolled once, at death.  There is deliberately
        # no fallback generation here: a corpse whose loot has been taken must
        # stay empty when the window is reopened.
        loot_data = spawn["loot_json"] if "loot_json" in spawn.keys() else ""
        raw_items: List[Tuple[int, int]] = []
        money = 0
        if loot_data:
            try:
                parsed = json.loads(loot_data)
                raw_items = [tuple(item) for item in parsed.get("items", [])]
                money = int(parsed.get("money", 0) or 0)
            except (json.JSONDecodeError, TypeError, ValueError):
                raw_items = []
                money = 0
        items = self._filter_loot_items(raw_items)
        slot_keys = self._loot_slot_keys(raw_items, items)
        empty = not items and money <= 0
        if empty:
            await self._set_loot_json(low_guid, "")
        # Group loot rules are applied when the window is first opened.  Above
        # threshold items become need/greed rolls or master-loot slots and are
        # reported to the client through the loot slot type.  Rolls keep the
        # raw corpse-row slot index; the window may filter empty slots, so the
        # UI index is mapped through ``slot_keys``.
        slot_types = {}
        group = (manager.group_for(int(self.player_guid or 0))
                 if manager is not None
                 and hasattr(manager, "group_for") else None)
        if group is not None and group.size > 1 and raw_items:
            qualities = self._item_qualities(raw_items)
            eligible = self._group_loot_eligible_sessions(group)
            await manager.begin_group_loot(
                group, low_guid, target_guid,
                int((self.character or {}).get("map", 0) or 0),
                raw_items, qualities, eligible)
            raw_types = manager.loot_slot_types(
                group, low_guid, raw_items, qualities,
                int(self.player_guid or 0))
            slot_types = {
                index: raw_types.get(slot_keys[index], 0)
                for index in range(len(items))
                if slot_keys[index] in raw_types
            }
        combat["loot_target"] = target_guid
        combat["loot_items"] = list(items)
        combat["loot_slot_keys"] = list(slot_keys)
        combat["loot_taken"] = []
        combat["loot_money"] = money
        await self._send_loot_response(target_guid, items, money, slot_types)
        if empty:
            # Nothing left: stop offering the loot cursor (TrinityCore removes
            # UNIT_DYNFLAG_LOOTABLE when the loot is emptied).
            await self._send_creature_fields(low_guid, {F.UNIT_DYNAMIC_FLAGS: 0})
            if manager is not None:
                manager.release_loot(low_guid, int(self.player_guid or 0))

    def _filter_loot_items(self, items) -> List[Tuple[int, int]]:
        """Drop loot entries the client has no item template for.

        The CMaNGOS dump predates the ICC/Ruby Sanctum item range, so some
        creature loot pointers reference items that are not in
        ``item_templates``.  Sending those makes the loot window show an
        unnamed question-mark entry and the client cannot build a tooltip.
        """
        cleaned: List[Tuple[int, int]] = []
        for item in items:
            try:
                item_entry, count = int(item[0]), int(item[1])
            except (TypeError, ValueError, IndexError):
                continue
            if item_entry > 0 and count > 0:
                cleaned.append((item_entry, count))
        if not cleaned:
            return []
        placeholders = ",".join("?" * len(cleaned))
        con = sqlite3.connect(self.config.database.world_path)
        try:
            valid = {
                int(row[0])
                for row in con.execute(
                    f"SELECT entry FROM item_templates WHERE entry IN ({placeholders})",
                    [entry for entry, _ in cleaned],
                )
            }
        finally:
            con.close()
        return [(entry, count) for entry, count in cleaned if entry in valid]

    @staticmethod
    def _loot_slot_keys(raw_items, filtered_items) -> List[int]:
        """Map filtered loot-window slots back to corpse-row slots.

        A rolled item is marked with a zero count in the corpse row so its
        index stays stable, but the client only receives non-empty slots.
        """
        keys: List[int] = []
        used: set[int] = set()
        for entry, count in filtered_items:
            match = -1
            for index, (raw_entry, raw_count) in enumerate(raw_items):
                if index in used:
                    continue
                try:
                    if int(raw_entry) == int(entry) \
                            and int(raw_count) == int(count):
                        match = index
                        break
                except (TypeError, ValueError):
                    continue
            if match < 0:
                match = len(keys)
            used.add(match)
            keys.append(match)
        return keys

    def _item_qualities(self, items) -> dict[int, int]:
        """Return ``{item_entry: quality}`` for the loot window."""
        entries = {int(item[0]) for item in items if int(item[0]) > 0}
        if not entries:
            return {}
        placeholders = ",".join("?" * len(entries))
        con = sqlite3.connect(self.config.database.world_path)
        try:
            return {
                int(row[0]): int(row[1] or 0)
                for row in con.execute(
                    f"SELECT entry, quality FROM item_templates "
                    f"WHERE entry IN ({placeholders})", list(entries))
            }
        finally:
            con.close()

    def _group_loot_eligible_sessions(self, group):
        """Online group members on this map and within group reward range."""
        manager = self._group_manager() if hasattr(
            self, "_group_manager") else None
        if manager is None:
            return []
        character = self.character or {}
        map_id = int(character.get("map", 0) or 0)
        origin = (float(character.get("x", 0.0) or 0.0),
                  float(character.get("y", 0.0) or 0.0),
                  float(character.get("z", 0.0) or 0.0))
        result = []
        for session in manager.member_sessions(group, map_id):
            if session is self:
                result.append(session)
                continue
            if session is None or not getattr(session, "character", None):
                continue
            try:
                if session._get_combat_state().get("dead"):
                    continue
            except Exception:
                pass
            other = session.character
            point = (float(other.get("x", 0.0) or 0.0),
                     float(other.get("y", 0.0) or 0.0),
                     float(other.get("z", 0.0) or 0.0))
            if math.dist(origin, point) <= 100.0:
                result.append(session)
        return result

    LOOT_TABLES = frozenset(("creature_loot_template", "gameobject_loot_template"))

    def _generate_loot(self, entry: int, loot_id: int = 0,
                       table: str = "creature_loot_template") -> List[Tuple[int, int]]:
        """Roll ``creature_loot_template`` (including references) once.

        ``table`` is ``gameobject_loot_template`` for chests, herbs and ore.

        ``loot_id`` is ``creature_template.LootId``; it defaults to the creature
        entry when the template has no separate loot id.  References are stored
        in the CMaNGOS dump as a negative ``mincountOrRef``: the absolute value
        is the ``reference_loot_template.entry`` to process.  Group ids select
        at most one row from a set, with CMaNGOS' shuffled explicit/equal-chance
        semantics.  Quest-only rows drop only for a player who needs the item;
        conditional rows are skipped because there is no condition engine.
        """
        if table not in self.LOOT_TABLES:
            raise ValueError(f"unknown loot table {table}")
        con = sqlite3.connect(self.config.database.world_path)
        try:
            rows = con.execute(
                "SELECT item, chance_or_quest_chance, group_id, "
                f"mincount_or_ref, maxcount, condition_id FROM {table} "
                "WHERE entry = ?",
                (int(loot_id or entry),),
            ).fetchall()
            rolled: List[Tuple[int, int]] = []
            self._roll_loot_rows(con, rows, rolled, depth=0)
        finally:
            con.close()
        return self._filter_loot_items(rolled)[:self.MAX_LOOT_ITEMS]

    @staticmethod
    def _loot_row_condition(row) -> int:
        return int(row[5] or 0) if len(row) > 5 else 0

    def _loot_row_usable(self, row) -> bool:
        """Skip conditional rows (no condition engine yet).

        Quest-only rows are filtered earlier by ``_loot_row_allowed``.
        """
        return self._loot_row_condition(row) == 0

    def _roll_loot_rows(self, con, rows, out: List[Tuple[int, int]],
                        depth: int) -> None:
        """Roll a list of loot rows into ``out`` as separate loot slots."""
        if depth > 8 or not rows:
            return
        rows = [row for row in rows if self._loot_row_allowed(row)]
        groups: Dict[int, list] = {}
        for row in rows:
            if not self._loot_row_usable(row):
                continue
            group_id = int(row[2] or 0)
            if group_id == 0:
                if self._loot_row_hits(row):
                    self._resolve_loot_row(con, row, out, depth)
            else:
                groups.setdefault(group_id, []).append(row)
        for group_rows in groups.values():
            chosen = self._pick_group_row(group_rows, out)
            if chosen is not None:
                self._resolve_loot_row(con, chosen, out, depth)

    @staticmethod
    def _loot_row_chance(row) -> float:
        # A negative chance marks a quest item; the magnitude is still the
        # drop percentage (see ``_loot_row_allowed`` for the quest check).
        return abs(float(row[1] or 0.0))

    def _loot_row_allowed(self, row) -> bool:
        """Quest items (negative chance) only drop for a player who needs them.

        ``LootItem::AllowedForPlayer``: the item must be an objective of an
        active, unfinished quest.
        """
        if float(row[1] or 0.0) >= 0.0 or int(row[3] or 0) < 0:
            return True
        needs = getattr(self, "_quest_needs_item", None)
        return bool(needs and needs(int(row[0])))

    def _loot_row_hits(self, row) -> bool:
        chance = self._loot_row_chance(row)
        if chance <= 0.0:
            return False                    # ungrouped 0% rows never drop
        if chance >= 100.0:
            return True
        return random.random() * 100.0 < chance

    def _pick_group_row(self, rows, out: List[Tuple[int, int]]):
        """Pick at most one row from a loot group (CMaNGOS LootGroup::Roll)."""
        explicit = [row for row in rows if self._loot_row_chance(row) > 0.0]
        if explicit:
            random.shuffle(explicit)
            roll = random.random() * 100.0
            for row in explicit:
                chance = self._loot_row_chance(row)
                if chance >= 100.0:
                    return row
                roll -= chance
                if roll < 0.0:
                    return row
        equal = [row for row in rows if self._loot_row_chance(row) == 0.0]
        if equal:
            random.shuffle(equal)
            existing = {int(item[0]) for item in out}
            for row in equal:
                item_entry = int(row[0] or 0)
                if item_entry in existing and random.randint(0, 1):
                    continue
                return row
        return None

    def _resolve_loot_row(self, con, row, out: List[Tuple[int, int]],
                          depth: int) -> None:
        item_entry = int(row[0] or 0)
        mincount_or_ref = int(row[3] or 0)
        maxcount = max(1, int(row[4] or 1))
        if mincount_or_ref < 0:
            reference_id = -mincount_or_ref
            if depth >= 8:
                return
            ref_rows = con.execute(
                "SELECT item, chance_or_quest_chance, group_id, "
                "mincount_or_ref, maxcount, condition_id "
                "FROM reference_loot_template WHERE entry = ?",
                (reference_id,),
            ).fetchall()
            if not ref_rows:
                return
            for _ in range(maxcount):
                self._roll_loot_rows(con, ref_rows, out, depth + 1)
            return
        if item_entry <= 0:
            return
        mincount = max(1, mincount_or_ref)
        count = random.randint(mincount, max(mincount, maxcount))
        out.append((item_entry, count))

    async def _set_loot_json(self, low_guid: int, payload: str) -> None:
        await run_db_write(
            execute_write, self.config.database.world_path,
            "UPDATE creature_spawns SET loot_json = ? WHERE guid = ?",
            (payload, int(low_guid)),
        )

    @staticmethod
    def _remaining_loot(combat) -> List[Tuple[int, int]]:
        taken = {int(index) for index in combat.get("loot_taken") or []}
        return [
            (int(item[0]), int(item[1]))
            for index, item in enumerate(combat.get("loot_items") or [])
            if index not in taken
        ]

    async def _persist_loot(self, combat) -> None:
        """Write the still-unlooted items/money back to the corpse row."""
        target = int(combat.get("loot_target") or 0)
        if not target:
            return
        if gameobject.is_gameobject_guid(target):
            self._persist_gameobject_loot(combat)
            return
        low_guid = creature.creature_low_guid(target)
        money = int(combat.get("loot_money") or 0)
        keys = combat.get("loot_slot_keys") or []
        taken = {int(index) for index in combat.get("loot_taken") or []}
        items = list(combat.get("loot_items") or [])
        if keys and len(keys) >= len(items):
            # Preserve the corpse-row indices used by in-flight group rolls;
            # only the taken slots become empty.
            con = sqlite3.connect(self.config.database.world_path)
            try:
                row = con.execute(
                    "SELECT loot_json FROM creature_spawns WHERE guid = ?",
                    (int(low_guid),)).fetchone()
            finally:
                con.close()
            raw_items = []
            if row and row[0]:
                try:
                    raw_items = list(json.loads(row[0]).get("items") or [])
                except (ValueError, TypeError, json.JSONDecodeError):
                    raw_items = []
            for index in taken:
                if index < len(keys) and 0 <= int(keys[index]) < len(raw_items):
                    raw_items[int(keys[index])] = [0, 0]
            has_items = any(
                int(item[0] or 0) > 0 and int(item[1] or 0) > 0
                for item in raw_items)
            if has_items or money:
                payload = json.dumps({"items": raw_items, "money": money})
            else:
                payload = ""
        else:
            remaining = self._remaining_loot(combat)
            if remaining or money:
                payload = json.dumps({"items": remaining, "money": money})
            else:
                payload = ""               # fully looted: corpse stays empty
        await self._set_loot_json(low_guid, payload)

    @staticmethod
    def _loot_money(spawn) -> int:
        """Copper from ``creature_template.MinLootGold``/``MaxLootGold``."""
        min_gold = int(spawn["min_loot_gold"] or 0) \
            if "min_loot_gold" in spawn.keys() else 0
        max_gold = int(spawn["max_loot_gold"] or 0) \
            if "max_loot_gold" in spawn.keys() else 0
        if max_gold <= 0:
            return 0
        if max_gold <= min_gold:
            return max_gold
        return random.randint(min_gold, max_gold)

    async def _send_loot_response(self, guid: int, items: List[Tuple[int, int]],
                                  money: int, slot_types=None) -> None:
        """Send ``SMSG_LOOT_RESPONSE`` with one entry per lootable item.

        ``displayInfoId`` comes from ``item_templates`` so the window shows the
        real icon; without a template the client falls back to a question mark.
        ``slot_types`` maps a loot slot index to a 3.3.5a ``LootSlotType``
        (0 allow, 1 roll ongoing, 2 master, 3 locked).
        """
        slot_types = slot_types or {}
        templates = {}
        if items:
            placeholders = ",".join("?" * len(items))
            con = sqlite3.connect(self.config.database.world_path)
            try:
                for row in con.execute(
                    f"SELECT entry, display_id, quality FROM item_templates "
                    f"WHERE entry IN ({placeholders})",
                    [entry for entry, _ in items],
                ):
                    templates[int(row[0])] = (int(row[1] or 0), int(row[2] or 0))
            finally:
                con.close()

        out = bytearray()
        out += struct.pack("<Q", int(guid))
        out += struct.pack("<B", 1)               # LOOT_METHOD_FREE_FOR_ALL
        out += struct.pack("<I", int(money))
        out += struct.pack("<B", len(items))
        for index, (item_entry, count) in enumerate(items):
            display_id, _quality = templates.get(int(item_entry), (0, 0))
            out += struct.pack("<B", index)       # list id
            out += struct.pack("<I", int(item_entry))
            out += struct.pack("<I", int(count))
            out += struct.pack("<I", display_id)
            out += struct.pack("<i", 0)           # random property seed
            out += struct.pack("<i", 0)           # random property id
            out += struct.pack("<B", int(slot_types.get(index, 0)) & 0xFF)
        await self.send_packet(Opcodes.SMSG_LOOT_RESPONSE, bytes(out))

    async def handle_autostore_loot_item(self, data: bytes):
        """``CMSG_AUTOSTORE_LOOT_ITEM``: move one loot slot into the backpack."""
        if not self.in_world or not self.character:
            return
        if len(data) < 1:
            return
        list_id = data[0]
        combat = self._get_combat_state()
        if combat.get("dead") or not combat["loot_target"]:
            return
        if list_id >= len(combat["loot_items"]):
            return
        if list_id in (combat.get("loot_taken") or []):
            return
        manager = (self._group_manager()
                   if hasattr(self, "_group_manager") else None)
        if manager is not None and hasattr(manager, "group_for"):
            group = manager.group_for(int(self.player_guid or 0))
            low_guid = creature.creature_low_guid(
                int(combat["loot_target"] or 0))
            keys = combat.get("loot_slot_keys") or []
            raw_slot = keys[list_id] if list_id < len(keys) else list_id
            if manager.is_loot_slot_blocked(group, low_guid, raw_slot):
                return
        item_entry, count = combat["loot_items"][list_id]
        changes = await run_db_write(
            character_db.add_item_to_inventory,
            self.config.database.world_path, int(self.player_guid),
            int(item_entry), int(count),
        )
        if not changes:
            # No free backpack slot: leave the item in the corpse and show the
            # client's "inventory is full" error (EQUIP_ERR_INV_FULL = 50).
            await self._send_inventory_failure(self.INVENTORY_FULL)
            return
        await self._deliver_item_changes(int(item_entry), changes)
        combat["loot_taken"].append(list_id)
        await self.broadcast_equipment_update()
        await self.send_packet(Opcodes.SMSG_LOOT_REMOVED, struct.pack("<B", list_id))
        await self._persist_loot(combat)
        await self._finish_loot_if_empty(combat)

    async def _loot_slot_entry(self, loot_target: int, slot: int):
        """Return ``(item_entry, count)`` for a live creature loot slot."""
        if gameobject.is_gameobject_guid(int(loot_target)):
            return 0, 0
        low_guid = creature.creature_low_guid(int(loot_target))
        con = sqlite3.connect(self.config.database.world_path)
        try:
            row = con.execute(
                "SELECT loot_json FROM creature_spawns WHERE guid = ?",
                (int(low_guid),)).fetchone()
        finally:
            con.close()
        if not row or not row[0]:
            return 0, 0
        try:
            items = list(json.loads(row[0]).get("items") or [])
            entry, count = items[int(slot)]
            return int(entry or 0), int(count or 0)
        except (ValueError, TypeError, IndexError, json.JSONDecodeError):
            return 0, 0

    async def _remove_rolled_loot_slot(self, loot_target: int, slot: int) -> None:
        """Mark a distributed loot slot as taken in the corpse row."""
        if gameobject.is_gameobject_guid(int(loot_target)):
            return
        low_guid = creature.creature_low_guid(int(loot_target))
        con = sqlite3.connect(self.config.database.world_path)
        try:
            row = con.execute(
                "SELECT loot_json FROM creature_spawns WHERE guid = ?",
                (int(low_guid),)).fetchone()
        finally:
            con.close()
        if row and row[0]:
            try:
                parsed = json.loads(row[0])
                items = list(parsed.get("items") or [])
                if 0 <= int(slot) < len(items):
                    items[int(slot)] = [0, 0]
                parsed["items"] = items
                await self._set_loot_json(low_guid, json.dumps(parsed))
            except (ValueError, TypeError, json.JSONDecodeError):
                pass
        # Every member with this corpse open must drop the slot too.
        manager = (self._group_manager()
                   if hasattr(self, "_group_manager") else None)
        group = (manager.group_for(int(self.player_guid or 0))
                 if manager is not None else None)
        sessions = (manager.member_sessions(group)
                    if group is not None else [self])
        for session in sessions:
            if not getattr(session, "in_world", False):
                continue
            combat = session._get_combat_state()
            if int(combat.get("loot_target") or 0) != int(loot_target):
                continue
            keys = combat.get("loot_slot_keys") or []
            if keys:
                try:
                    ui_slot = keys.index(int(slot))
                except ValueError:
                    continue
            else:
                if int(slot) >= len(combat.get("loot_items") or []):
                    continue
                ui_slot = int(slot)
            taken = combat.setdefault("loot_taken", [])
            if int(ui_slot) not in taken:
                taken.append(int(ui_slot))
            try:
                await session.send_packet(
                    Opcodes.SMSG_LOOT_REMOVED,
                    struct.pack("<B", int(ui_slot)))
                await session._finish_loot_if_empty(combat)
            except Exception:
                pass

    async def _award_rolled_loot(self, loot_target: int, low_guid: int,
                                 item_entry: int, count: int,
                                 slot: int) -> bool:
        """Store a won/master-looted item and remove it from the corpse."""
        if not self.in_world or not self.character:
            return False
        changes = await run_db_write(
            character_db.add_item_to_inventory,
            self.config.database.world_path, int(self.player_guid),
            int(item_entry), int(count),
        )
        if not changes:
            await self._send_inventory_failure(self.INVENTORY_FULL)
            return False
        await self._deliver_item_changes(int(item_entry), changes)
        await self.broadcast_equipment_update()
        await self._remove_rolled_loot_slot(int(loot_target), int(slot))
        return True

    async def handle_loot_master_give(self, data: bytes):
        """``CMSG_LOOT_MASTER_GIVE``: master looter hands a slot to a member."""
        if not self.in_world or not self.character or len(data) < 17:
            return
        loot_target, slot, target_guid = struct.unpack_from("<QBQ", data, 0)
        manager = (self._group_manager()
                   if hasattr(self, "_group_manager") else None)
        if manager is None:
            return
        combat = self._get_combat_state()
        if int(combat.get("loot_target") or 0) != int(loot_target):
            return
        keys = combat.get("loot_slot_keys") or []
        raw_slot = keys[slot] if int(slot) < len(keys) else int(slot)
        await manager.master_loot_give(
            self, int(loot_target), int(raw_slot), int(target_guid))

    async def _close_loot(self) -> None:
        """Forget the open loot window without changing the stored loot."""
        combat = self._get_combat_state()
        target = int(combat.get("loot_target") or 0)
        if target and gameobject.is_gameobject_guid(target):
            runtime = self._go_runtime()
            low_guid = gameobject.gameobject_low_guid(target)
            if runtime.looter.get(low_guid) == int(self.player_guid or 0):
                runtime.looter.pop(low_guid, None)
            await self._set_looting_flag(False)
        elif target and hasattr(self, "_group_manager"):
            manager = self._group_manager()
            if manager is not None:
                manager.release_loot(
                    creature.creature_low_guid(target),
                    int(self.player_guid or 0))
        combat["loot_target"] = 0
        combat["loot_items"] = []
        combat["loot_taken"] = []
        combat["loot_slot_keys"] = []
        combat["loot_money"] = 0

    async def handle_loot_release(self, data: bytes):
        """``CMSG_LOOT_RELEASE``: close the loot window without taking items."""
        if len(data) >= 8:
            guid = struct.unpack_from("<Q", data, 0)[0]
            await self.send_packet(
                Opcodes.SMSG_LOOT_RELEASE_RESPONSE,
                struct.pack("<Q", guid) + struct.pack("<B", 1),
            )
        # Clear in-memory state but KEEP loot in database for later.  Anything
        # already taken was persisted as soon as it was taken.
        combat = self._get_combat_state()
        target = int(combat.get("loot_target") or 0)
        if target and gameobject.is_gameobject_guid(target):
            await self._release_gameobject_loot(target, emptied=False)
        await self._close_loot()

    async def handle_loot_money(self, data: bytes):
        """``CMSG_LOOT_MONEY``: take the corpse's coins."""
        if not self.in_world or not self.character:
            return
        combat = self._get_combat_state()
        money = int(combat.get("loot_money") or 0)
        if combat.get("dead") or not combat["loot_target"] or money <= 0:
            return
        combat["loot_money"] = 0
        total = int(self.character.get("money", 0)) + money
        self.character["money"] = total
        await run_db_write(
            character_db.set_character_money,
            self.config.database.world_path, int(self.player_guid), total,
        )
        await self._send_player_fields({F.PLAYER_FIELD_COINAGE: total & 0xFFFFFFFF})
        await self.send_packet(
            Opcodes.SMSG_LOOT_MONEY_NOTIFY,
            struct.pack("<IB", money & 0xFFFFFFFF, 1),
        )
        await self._persist_loot(combat)
        await self._finish_loot_if_empty(combat)

    async def _finish_loot_if_empty(self, combat) -> bool:
        """Close the window and stop the corpse being lootable once emptied.

        TrinityCore removes ``UNIT_DYNFLAG_LOOTABLE`` when the last item/money
        is taken, so the client no longer offers a loot cursor on that corpse.
        """
        if self._remaining_loot(combat) or int(combat.get("loot_money") or 0) > 0:
            return False
        target = int(combat.get("loot_target") or 0)
        if target and gameobject.is_gameobject_guid(target):
            await self._close_loot()
            await self._release_gameobject_loot(target, emptied=True)
            return True
        if target:
            await self._send_creature_fields(
                creature.creature_low_guid(target),
                {F.UNIT_DYNAMIC_FLAGS: 0},
            )
        await self._close_loot()
        return True
