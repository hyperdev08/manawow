"""Loot generation, rolls, persistence and loot opcodes."""
from __future__ import annotations

import json
import random
import sqlite3
import struct

from typing import Dict, List, Tuple

from src.protocol.opcodes import Opcodes
from src.world import character_db
from src.world import creature
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
        items: List[Tuple[int, int]] = []
        money = 0
        if loot_data:
            try:
                parsed = json.loads(loot_data)
                items = [tuple(item) for item in parsed.get("items", [])]
                money = int(parsed.get("money", 0) or 0)
            except (json.JSONDecodeError, TypeError, ValueError):
                items = []
                money = 0
        items = self._filter_loot_items(items)
        empty = not items and money <= 0
        if empty:
            self._set_loot_json(low_guid, "")
        combat["loot_target"] = target_guid
        combat["loot_items"] = list(items)
        combat["loot_taken"] = []
        combat["loot_money"] = money
        await self._send_loot_response(target_guid, items, money)
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

    def _generate_loot(self, entry: int, loot_id: int = 0) -> List[Tuple[int, int]]:
        """Roll ``creature_loot_template`` (including references) once.

        ``loot_id`` is ``creature_template.LootId``; it defaults to the creature
        entry when the template has no separate loot id.  References are stored
        in the CMaNGOS dump as a negative ``mincountOrRef``: the absolute value
        is the ``reference_loot_template.entry`` to process.  Group ids select
        at most one row from a set, with CMaNGOS' shuffled explicit/equal-chance
        semantics.  Quest-only rows drop only for a player who needs the item;
        conditional rows are skipped because there is no condition engine.
        """
        con = sqlite3.connect(self.config.database.world_path)
        try:
            rows = con.execute(
                "SELECT item, chance_or_quest_chance, group_id, "
                "mincount_or_ref, maxcount, condition_id "
                "FROM creature_loot_template WHERE entry = ?",
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

    def _set_loot_json(self, low_guid: int, payload: str) -> None:
        con = sqlite3.connect(self.config.database.world_path)
        try:
            con.execute(
                "UPDATE creature_spawns SET loot_json = ? WHERE guid = ?",
                (payload, int(low_guid)),
            )
            con.commit()
        finally:
            con.close()

    @staticmethod
    def _remaining_loot(combat) -> List[Tuple[int, int]]:
        taken = {int(index) for index in combat.get("loot_taken") or []}
        return [
            (int(item[0]), int(item[1]))
            for index, item in enumerate(combat.get("loot_items") or [])
            if index not in taken
        ]

    def _persist_loot(self, combat) -> None:
        """Write the still-unlooted items/money back to the corpse row."""
        target = int(combat.get("loot_target") or 0)
        if not target:
            return
        remaining = self._remaining_loot(combat)
        money = int(combat.get("loot_money") or 0)
        if remaining or money:
            payload = json.dumps({"items": remaining, "money": money})
        else:
            payload = ""                   # fully looted: corpse stays empty
        self._set_loot_json(creature.creature_low_guid(target), payload)

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
                                  money: int) -> None:
        """Send ``SMSG_LOOT_RESPONSE`` with one entry per lootable item.

        ``displayInfoId`` comes from ``item_templates`` so the window shows the
        real icon; without a template the client falls back to a question mark.
        """
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
            out += struct.pack("<B", 0)           # LOOT_FILTER_TYPE_LOOT
        await self.send_packet(Opcodes.SMSG_LOOT_RESPONSE, bytes(out))

    async def handle_autostore_loot_item(self, data: bytes):
        """``CMSG_AUTOSTORE_LOOT_ITEM``: move one loot slot into the backpack."""
        if not self.in_world or not self.character:
            return
        if len(data) < 1:
            return
        list_id = data[0]
        combat = self._get_combat_state()
        if not combat["loot_target"]:
            return
        if list_id >= len(combat["loot_items"]):
            return
        if list_id in (combat.get("loot_taken") or []):
            return
        item_entry, count = combat["loot_items"][list_id]
        changes = character_db.add_item_to_inventory(
            self.config.database.world_path, int(self.player_guid),
            int(item_entry), int(count),
        )
        if not changes:
            # No free backpack slot: leave the item in the corpse and show the
            # client's "inventory is full" error (EQUIP_ERR_INV_FULL = 50).
            await self._send_inventory_failure(self.INVENTORY_FULL)
            return
        inventory = self.character.setdefault("inventory", {})
        for change in changes:
            slot = int(change["slot"])
            if change["created"]:
                item_guid = F.ITEM_HIGH_GUID | (int(change["id"]) & 0xFFFFFFFF)
                item_dict = {
                    "guid": item_guid,
                    "id": change["id"],
                    "item_entry": int(item_entry),
                    "stack_count": change["stack_count"],
                    "durability": change["durability"],
                    "max_durability": change["max_durability"],
                    "slot": slot,
                    "owner_guid": int(self.player_guid),
                    "enchantment_1": 0,
                }
                inventory[slot] = item_dict
                # The client cannot show a bag item whose object does not
                # exist, so the create block has to precede the field update
                # that references it.
                await self._send_item_create_block(item_dict)
            else:
                item_dict = inventory.get(slot)
                if item_dict is not None:
                    item_dict["stack_count"] = change["stack_count"]
                    opcode, payload = object_update.build_item_values_update(
                        item_dict,
                        {F.ITEM_FIELD_STACK_COUNT: int(change["stack_count"])},
                    )
                    await self.send_packet(opcode, payload)
            client_slot = self._internal_slot_to_client(slot)
            await self.send_packet(
                Opcodes.SMSG_ITEM_PUSH_RESULT,
                login_packets.build_item_push_result(
                    int(self.player_guid), int(item_entry), 255, client_slot,
                    count=change["added"], total_count=change["stack_count"],
                    received=change["added"], created=0, send_chat_message=1,
                ),
            )
        combat["loot_taken"].append(list_id)
        await self.broadcast_equipment_update()
        if hasattr(self, "_quest_items_changed"):
            await self._quest_items_changed([int(item_entry)])
        await self.send_packet(Opcodes.SMSG_LOOT_REMOVED, struct.pack("<B", list_id))
        self._persist_loot(combat)
        await self._finish_loot_if_empty(combat)

    async def _close_loot(self) -> None:
        """Forget the open loot window without changing the stored loot."""
        combat = self._get_combat_state()
        target = int(combat.get("loot_target") or 0)
        if target and hasattr(self, "_group_manager"):
            manager = self._group_manager()
            if manager is not None:
                manager.release_loot(
                    creature.creature_low_guid(target),
                    int(self.player_guid or 0))
        combat["loot_target"] = 0
        combat["loot_items"] = []
        combat["loot_taken"] = []
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
        await self._close_loot()

    async def handle_loot_money(self, data: bytes):
        """``CMSG_LOOT_MONEY``: take the corpse's coins."""
        if not self.in_world or not self.character:
            return
        combat = self._get_combat_state()
        money = int(combat.get("loot_money") or 0)
        if not combat["loot_target"] or money <= 0:
            return
        combat["loot_money"] = 0
        total = int(self.character.get("money", 0)) + money
        self.character["money"] = total
        con = sqlite3.connect(self.config.database.world_path)
        try:
            con.execute("UPDATE characters SET money = ? WHERE guid = ?",
                        (total, int(self.player_guid)))
            con.commit()
        finally:
            con.close()
        await self._send_player_fields({F.PLAYER_FIELD_COINAGE: total & 0xFFFFFFFF})
        await self.send_packet(
            Opcodes.SMSG_LOOT_MONEY_NOTIFY,
            struct.pack("<IB", money & 0xFFFFFFFF, 1),
        )
        self._persist_loot(combat)
        await self._finish_loot_if_empty(combat)

    async def _finish_loot_if_empty(self, combat) -> bool:
        """Close the window and stop the corpse being lootable once emptied.

        TrinityCore removes ``UNIT_DYNFLAG_LOOTABLE`` when the last item/money
        is taken, so the client no longer offers a loot cursor on that corpse.
        """
        if self._remaining_loot(combat) or int(combat.get("loot_money") or 0) > 0:
            return False
        target = int(combat.get("loot_target") or 0)
        if target:
            await self._send_creature_fields(
                creature.creature_low_guid(target),
                {F.UNIT_DYNAMIC_FLAGS: 0},
            )
        await self._close_loot()
        return True
