"""Inventory swaps, autoequip and item destruction."""
from __future__ import annotations

import sqlite3
import struct

from src.protocol.opcodes import Opcodes
from src.world import character_db
from src.world import object_update
from src.world import update_fields as F

class InventoryMixin:
    # ------------------------------------------------------------------
    # Item handlers
    # ------------------------------------------------------------------
    # Item.dbc inventoryType -> default equipment slots, most preferred first.
    # Slots are the client's player-container slots: 0..18 are equipment
    # (19 = bag 1, ..., 22 = bag 4, 23..38 = backpack), so only 0..18 are valid
    # auto-equip destinations.  Missing inventory types (trade goods, bags,
    # quest items, ...) simply cannot be equipped.
    _EQUIP_SLOTS_BY_INVTYPE = {
        1: (0,),        # head
        2: (1,),        # neck
        3: (2,),        # shoulder
        4: (3,),        # shirt
        5: (4,),        # chest
        20: (4,),       # robe
        6: (5,),        # waist
        7: (6,),        # legs
        8: (7,),        # feet
        9: (8,),        # wrists
        10: (9,),       # hands
        11: (10, 11),   # finger
        12: (12, 13),   # trinket
        13: (15, 16),   # one-hand weapon: main hand first
        16: (14,),      # back
        17: (15,),      # two-hand weapon
        19: (18,),      # tabard
        21: (15,),      # main hand
        22: (16,),      # off hand
        23: (16,),      # held in off hand
        14: (16,),      # shield
        15: (17,),      # bow
        25: (17,),      # thrown
        26: (17,),      # ranged right (gun/crossbow/wand)
        28: (17,),      # relic
    }

    # ``SMSG_INVENTORY_CHANGE_FAILURE``: uint8 result, two item guids, bag type.
    # Values from 3.3.5a ``InventoryResult`` (ItemDefines.h).
    ITEM_DOESNT_GO_TO_SLOT = 3

    async def _send_inventory_failure(self, error: int = ITEM_DOESNT_GO_TO_SLOT):
        await self.send_packet(
            Opcodes.SMSG_INVENTORY_CHANGE_FAILURE,
            struct.pack("<BQQB", error, 0, 0, 0),
        )

    @staticmethod
    def _client_slot_to_internal(slot: int) -> int:
        """Map a client inventory slot to our internal ``character_items.slot``.

        The client sends equipment slots 0..22 (19 equip + 4 bags) and backpack
        slots 23..38 as if they were one array, with 23 = backpack slot 0.  We
        store backpack slots as ``256 + (0..15)`` to keep them distinct from the
        bag slots 19..22.
        """
        if 0 <= slot <= 22:
            return slot
        if 23 <= slot <= 38:            # backpack slot 0..15
            return 256 + (slot - 23)
        return -1

    @staticmethod
    def _internal_slot_to_client(slot: int) -> int:
        """Inverse of ``_client_slot_to_internal``."""
        if 0 <= slot <= 22:
            return slot
        if 256 <= slot <= 271:
            return 23 + (slot - 256)
        return -1

    async def handle_swap_inv_item(self, data: bytes):
        """``CMSG_SWAP_INV_ITEM``: swap two equipped/inventory slots.

        Format: uint8 dstSlot, uint8 srcSlot.  Slots are the player container:
        0..22 equipment + bag slots, 23..38 backpack.  The client is optimistic
        about clearing the cursor but does not move the item itself: on success
        we persist the swap and send a player ``UPDATETYPE_VALUES`` update so
        the client's inventory/visible-item mirrors change, which fires its
        ``OnUpdateInventoryComponent`` (model) and bag-update handlers.  Failure
        triggers SMSG_INVENTORY_CHANGE_FAILURE.
        """
        if not self.in_world or len(data) < 2:
            return
        dst_slot, src_slot = data[0], data[1]
        src_int = self._client_slot_to_internal(src_slot)
        dst_int = self._client_slot_to_internal(dst_slot)
        if src_int < 0 or dst_int < 0:
            self.log.warning(f"CMSG_SWAP_INV_ITEM: invalid slots {src_slot}->{dst_slot}")
            await self._send_inventory_failure()
            return
        ok = character_db.swap_character_items(
            self.config.database.world_path, self.player_guid, src_int, dst_int
        )
        if ok:
            # Re-read the inventory so the in-memory mirror always matches the
            # database.  Swapping an *empty* source with a filled destination is
            # the common case (auto-equip moves a bag item into an empty slot),
            # and hand-copying the dict here would leave the source item where
            # it was while the DB already moved it.
            inventory = character_db.get_character_inventory(
                self.config.database.world_path, int(self.player_guid)
            )
            mutable_inventory = {}
            for slot, item in inventory.items():
                item_dict = dict(item)
                item_dict["guid"] = F.ITEM_HIGH_GUID | (
                    int(item_dict["id"]) & 0xFFFFFFFF
                )
                item_dict["owner_guid"] = int(self.player_guid)
                mutable_inventory[int(slot)] = item_dict
            self.character["inventory"] = mutable_inventory
            # Re-derive stats/pools from the new equipment before broadcasting.
            self._apply_combat_stats()
            self.log.info(f"Swapped client slots {src_slot}->{dst_slot} (internal {src_int}->{dst_int})")

            # Send the player fields values update (uncompressed) so the client's
            # mirror of INV_SLOT_HEAD / PACK_SLOT_1 / VISIBLE_ITEM stays in sync
            # and its OnUpdateInventoryComponent handler rebuilds the model.
            # Peers need the same update for the armour/weapon visuals.
            await self.broadcast_equipment_update()
        else:
            await self._send_inventory_failure()

    async def handle_swap_item(self, data: bytes):
        """``CMSG_SWAP_ITEM``: move item between bag and equipment (or bag<->bag).

        Format: uint8 dstBag, uint8 dstSlot, uint8 srcBag, uint8 srcSlot.
        Bag 255 (also accepted as 0) = player (equipment 0..22 + backpack
        23..38), 1..4 = bags 1..4.  Only the player container is implemented for
        now; real bags return failure.
        """
        if not self.in_world or len(data) < 4:
            return
        dst_bag, dst_slot, src_bag, src_slot = data[0], data[1], data[2], data[3]
        if src_bag not in (0, 0xFF) or dst_bag not in (0, 0xFF):
            self.log.debug(f"CMSG_SWAP_ITEM: non-equipment bags not implemented ({src_bag}->{dst_bag})")
            await self._send_inventory_failure()
            return
        await self.handle_swap_inv_item(bytes([dst_slot, src_slot]))

    async def handle_autoequip_item(self, data: bytes):
        """``CMSG_AUTOEQUIP_ITEM``: right-click an item to equip it.

        Format: uint8 srcBag, uint8 srcSlot.  The client sends bag 255 (or 0)
        with player-container slots (23..38 = backpack) for backpack items and
        bag 1..4 with 0..(bagsize-1) for real bags.  Only the player container
        is implemented; anything else fails like a full bag.

        The item's ``Item.dbc`` inventory type picks the destination slot: an
        empty candidate first, otherwise the most preferred one (so a one-hand
        weapon replaces whatever is in the main hand).  The actual move reuses
        ``CMSG_SWAP_INV_ITEM`` so the client gets exactly the same values update
        it gets from dragging the item.
        """
        if not self.in_world or len(data) < 2:
            return
        src_bag, src_slot = data[0], data[1]
        if src_bag not in (0, 0xFF):
            self.log.debug(
                f"CMSG_AUTOEQUIP_ITEM: bag {src_bag} not implemented"
            )
            await self._send_inventory_failure()
            return

        src_int = self._client_slot_to_internal(src_slot)
        inv = self.character.get("inventory", {})
        item = inv.get(src_int) if src_int >= 0 else None
        if item is None:
            self.log.debug(f"CMSG_AUTOEQUIP_ITEM: empty slot {src_slot}")
            await self._send_inventory_failure()
            return

        template = character_db.get_item_template(
            self.config.database.world_path, int(item["item_entry"])
        )
        candidates = ()
        if template is not None:
            candidates = self._EQUIP_SLOTS_BY_INVTYPE.get(
                int(template["inventory_type"]), ()
            )
        if not candidates:
            self.log.debug(
                f"CMSG_AUTOEQUIP_ITEM: entry {item['item_entry']} is not equippable"
            )
            await self._send_inventory_failure()
            return

        dst_slot = next((slot for slot in candidates if slot not in inv), candidates[0])
        if dst_slot == src_int:
            return
        self.log.info(
            f"Auto-equipping client slot {src_slot} -> {dst_slot} "
            f"(entry {item['item_entry']})"
        )
        await self.handle_swap_inv_item(bytes([dst_slot, src_slot]))

    async def handle_destroy_item(self, data: bytes):
        """``CMSG_DESTROYITEM``: delete an item from inventory.

        Format: uint8 container id (255 = player container), uint8 slot,
        uint32 count.  TrinityCore treats ``count == 0`` as "destroy the whole
        stack"; the 3.3.5 client sends 0 when the player drags an item out of
        the bag and confirms deletion.
        """
        if not self.in_world or len(data) < 6:
            return
        bag, slot = data[0], data[1]
        count = struct.unpack_from("<I", data, 2)[0]
        if bag not in (0, 0xFF):
            self.log.debug(f"CMSG_DESTROYITEM: non-equipment bag={bag} slot={slot} ignored")
            await self._send_inventory_failure()
            return
        internal_slot = self._client_slot_to_internal(slot)
        if internal_slot < 0:
            self.log.debug(f"CMSG_DESTROYITEM: invalid slot {slot}")
            await self._send_inventory_failure()
            return
        inv = self.character.get("inventory", {})
        item = inv.get(internal_slot)
        if item is None:
            self.log.debug(f"CMSG_DESTROYITEM: slot {slot} empty")
            await self._send_inventory_failure()
            return
        current = max(1, int(item.get("stack_count", 1)))
        if count == 0 or count >= current:
            # Whole item (or whole stack).
            character_db.delete_character_item(
                self.config.database.world_path, self.player_guid, internal_slot
            )
            del inv[internal_slot]
            # Worn equipment may have been destroyed: recompute the derived
            # stats before the values update goes out.
            self._apply_combat_stats()
            await self.send_packet(
                Opcodes.SMSG_DESTROY_OBJECT,
                object_update.build_item_destroy_object(int(item["guid"])),
            )
            # Clear the slot on this client and on every peer that can see the
            # character (destroying worn armour has to remove the model too).
            await self.broadcast_equipment_update()
            if hasattr(self, "_quest_items_changed"):
                await self._quest_items_changed([int(item["item_entry"])])
            self.log.info(f"Destroyed item in slot {slot} for guid {self.player_guid}")
            return

        # Partial destroy: decrement the stack and update the item object so
        # the bag count changes immediately.
        new_count = current - int(count)
        con = sqlite3.connect(self.config.database.world_path)
        try:
            con.execute(
                "UPDATE character_items SET stack_count = ? "
                "WHERE character_guid = ? AND slot = ?",
                (new_count, self.player_guid, internal_slot),
            )
            con.commit()
        finally:
            con.close()
        item["stack_count"] = new_count
        opcode, payload = object_update.build_item_values_update(
            item, {F.ITEM_FIELD_STACK_COUNT: new_count}
        )
        await self.send_packet(opcode, payload)
        if hasattr(self, "_quest_items_changed"):
            await self._quest_items_changed([int(item["item_entry"])])
        self.log.info(f"Reduced stack in slot {slot} to {new_count}")

    async def _send_item_create_block(self, item: dict) -> None:
        """Create an item object on this client only.

        Looted items are private, so unlike equipment the block goes to the
        owner and never to peers.
        """
        opcode, payload = object_update.build_item_update(item)
        await self.send_packet(opcode, payload)
