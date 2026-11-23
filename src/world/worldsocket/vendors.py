"""NPC vendors: item list, buying and selling."""
from __future__ import annotations

import sqlite3

from src.protocol.opcodes import Opcodes
from src.world import character_db
from src.world import creature
from src.world import vendor


class VendorsMixin:
    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _vendor_spawn(self, guid: int):
        """The spawn behind ``guid`` if it is a vendor, else ``None``."""
        spawn = creature.get_creature_spawn(
            self.config.database.world_path, creature.creature_low_guid(guid)
        )
        if spawn is None or not int(spawn["npc_flags"]) & vendor.NPC_FLAG_VENDOR:
            return None
        return spawn

    def _vendor_in_range(self, guid: int) -> bool:
        low_guid = creature.creature_low_guid(guid)
        return self._distance(
            self._player_position(), self._creature_position(low_guid)
        ) <= vendor.VENDOR_INTERACT_DISTANCE

    async def _send_vendor_list(self, guid: int, spawn) -> None:
        low_guid = creature.creature_low_guid(guid)
        items = vendor.get_vendor_items(self.config.database.world_path, int(spawn["entry"]))
        await self.send_packet(
            Opcodes.SMSG_LIST_INVENTORY,
            vendor.build_list_inventory(
                guid, items,
                int(self.character.get("race", 1)),
                int(self.character.get("class", 1)),
                lambda item: vendor.STOCK.remaining(low_guid, item),
            ),
        )
        self.log.info(f"Vendor list for '{spawn['name']}' ({guid}): {len(items)} items")

    # ------------------------------------------------------------------
    # Opcode handlers
    # ------------------------------------------------------------------
    async def handle_list_inventory(self, data: bytes):
        """``CMSG_LIST_INVENTORY``: right-click on a vendor without gossip."""
        if not self.in_world or not self.character or len(data) < 8:
            return
        guid = int.from_bytes(data[:8], "little")
        spawn = self._vendor_spawn(guid)
        if spawn is None:
            self.log.debug(f"List inventory for non-vendor {guid}")
            return
        await self._send_vendor_list(guid, spawn)

    async def handle_buy_item(self, data: bytes, in_slot: bool = False):
        """``CMSG_BUY_ITEM`` / ``CMSG_BUY_ITEM_IN_SLOT``.

        ``count`` is the number of vendor bundles: buying one "Refreshing
        Spring Water" bundle yields ``buy_count`` (5) bottles for ``buy_price``.
        Items always go to the backpack; the requested bag slot is ignored.
        """
        if not self.in_world or not self.character:
            return
        parsed = (vendor.parse_buy_item_in_slot if in_slot else vendor.parse_buy_item)(data)
        if parsed is None:
            return
        guid, item_entry, vendor_slot, count = parsed
        count = max(1, int(count))

        async def fail(error: int) -> None:
            await self.send_packet(
                Opcodes.SMSG_BUY_FAILED, vendor.build_buy_failed(guid, item_entry, error)
            )

        spawn = self._vendor_spawn(guid)
        if spawn is None:
            await fail(vendor.BUY_ERR_CANT_FIND_ITEM)
            return
        if not self._vendor_in_range(guid):
            await fail(vendor.BUY_ERR_DISTANCE_TOO_FAR)
            return
        items = vendor.get_vendor_items(self.config.database.world_path, int(spawn["entry"]))
        index = int(vendor_slot) - 1          # the client counts slots from 1
        if not 0 <= index < len(items) or int(items[index]["item"]) != int(item_entry):
            await fail(vendor.BUY_ERR_CANT_FIND_ITEM)
            return
        item = items[index]
        if not vendor.visible_to(item, int(self.character.get("race", 1)),
                                 int(self.character.get("class", 1))):
            await fail(vendor.BUY_ERR_CANT_FIND_ITEM)
            return

        low_guid = creature.creature_low_guid(guid)
        left = vendor.STOCK.remaining(low_guid, item)
        if left != vendor.UNLIMITED:
            if left <= 0:
                await fail(vendor.BUY_ERR_ITEM_SOLD_OUT)
                return
            if count > left:
                await fail(vendor.BUY_ERR_ITEM_ALREADY_SOLD)
                return

        price = int(item.get("buy_price") or 0) * count
        if price > vendor.MAX_MONEY_AMOUNT or price > int(self.character.get("money", 0)):
            await fail(vendor.BUY_ERR_NOT_ENOUGHT_MONEY)
            return

        quantity = max(1, int(item.get("buy_count") or 1)) * count
        changes = character_db.add_item_to_inventory(
            self.config.database.world_path, int(self.player_guid),
            int(item_entry), quantity,
        )
        if not changes:
            # Not enough backpack space: the client shows "Inventory is full".
            await self._send_inventory_failure(self.INVENTORY_FULL)
            return

        if price:
            await self._modify_money(-price)
        left = vendor.STOCK.take(low_guid, item, count)
        await self._deliver_item_changes(int(item_entry), changes)
        await self.broadcast_equipment_update()
        await self.send_packet(
            Opcodes.SMSG_BUY_ITEM, vendor.build_buy_item(guid, index, left, count)
        )
        self.log.info(
            f"Bought {quantity}x item {item_entry} from '{spawn['name']}' for {price} copper"
        )

    async def handle_sell_item(self, data: bytes):
        """``CMSG_SELL_ITEM``: sell ``count`` of a backpack item (0 = whole stack)."""
        if not self.in_world or not self.character:
            return
        parsed = vendor.parse_sell_item(data)
        if parsed is None:
            return
        guid, item_guid, count = parsed

        async def fail(error: int) -> None:
            await self.send_packet(
                Opcodes.SMSG_SELL_ITEM, vendor.build_sell_failed(guid, item_guid, error)
            )

        spawn = self._vendor_spawn(guid)
        if spawn is None or not self._vendor_in_range(guid):
            await fail(vendor.SELL_ERR_CANT_FIND_VENDOR)
            return

        inventory = self.character.get("inventory", {})
        slot, item = next(
            ((s, it) for s, it in inventory.items() if int(it.get("guid", 0)) == int(item_guid)),
            (None, None),
        )
        if item is None:
            await fail(vendor.SELL_ERR_CANT_FIND_ITEM)
            return
        if not 256 <= int(slot) <= 271:
            # Worn gear and bags must be moved into the backpack first.
            await fail(vendor.SELL_ERR_CANT_SELL_ITEM)
            return

        stack = max(1, int(item.get("stack_count", 1)))
        if count == 0:
            count = stack
        if count > stack:
            await fail(vendor.SELL_ERR_CANT_SELL_ITEM)
            return
        sell_price = self._item_sell_price(int(item["item_entry"]))
        if sell_price <= 0:
            await fail(vendor.SELL_ERR_CANT_SELL_ITEM)
            return

        removed = await self._remove_item_from_slot(int(slot), count)
        if removed <= 0:
            await fail(vendor.SELL_ERR_CANT_FIND_ITEM)
            return
        await self._modify_money(sell_price * removed)
        self.log.info(
            f"Sold {removed}x item {item['item_entry']} to '{spawn['name']}' "
            f"for {sell_price * removed} copper"
        )

    def _item_sell_price(self, entry: int) -> int:
        con = sqlite3.connect(self.config.database.world_path)
        try:
            row = con.execute(
                "SELECT sell_price FROM item_templates WHERE entry = ?", (int(entry),)
            ).fetchone()
        finally:
            con.close()
        return int(row[0] or 0) if row else 0
