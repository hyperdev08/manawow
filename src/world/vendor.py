"""Vendor data, limited stock and the 3.3.5a vendor packets.

The layouts follow TrinityCore 3.3.5 ``WorldSession::SendListInventory`` /
``Player::BuyItemFromVendorSlot`` / ``WorldSession::HandleSellItemOpcode``.

Vendor slots on the wire are 1-based indexes into the vendor's *full* item
list (the order the client gets them in), so items hidden from one player
(other faction, bind-on-pickup gear for another class) never shift the index
of the items after them.
"""
from __future__ import annotations

import sqlite3
import struct
import time
from typing import Dict, List, Optional, Tuple

# UNIT_NPC_FLAG_VENDOR and the gossip option type CMaNGOS uses for "browse goods".
NPC_FLAG_VENDOR = 0x80
GOSSIP_OPTION_VENDOR = 3
GOSSIP_ICON_VENDOR = 1
# Index of the "browse your goods" option the server adds to gossip menus that
# lack one (gossip-flagged vendors whose menu only has chat options).
VENDOR_GOSSIP_OPTION_INDEX = 0xFFFF


def vendor_gossip_option() -> dict:
    """The "browse your goods" row ``build_gossip_message`` expects."""
    return {
        "option_index": VENDOR_GOSSIP_OPTION_INDEX,
        "icon": GOSSIP_ICON_VENDOR,
        "text": "I want to browse your goods.",
    }

MAX_VENDOR_ITEMS = 150          # TrinityCore MAX_VENDOR_ITEMS
MAX_MONEY_AMOUNT = 0x7FFFFFFF   # money is an int32 player field
VENDOR_INTERACT_DISTANCE = 15.0  # generous: client positions lag a little
UNLIMITED = 0xFFFFFFFF

# BuyResult (SharedDefines.h)
BUY_ERR_CANT_FIND_ITEM = 0
BUY_ERR_ITEM_ALREADY_SOLD = 1
BUY_ERR_NOT_ENOUGHT_MONEY = 2
BUY_ERR_DISTANCE_TOO_FAR = 5
BUY_ERR_ITEM_SOLD_OUT = 7
BUY_ERR_CANT_CARRY_MORE = 8

# SellResult
SELL_ERR_CANT_FIND_ITEM = 1
SELL_ERR_CANT_SELL_ITEM = 2
SELL_ERR_CANT_FIND_VENDOR = 3
SELL_ERR_YOU_DONT_OWN_THAT_ITEM = 4

# Item.dbc flags2 / bonding
ITEM_FLAG2_HORDE_ONLY = 0x1
ITEM_FLAG2_ALLIANCE_ONLY = 0x2
BIND_WHEN_PICKED_UP = 1

ALLIANCE_RACES = frozenset((1, 3, 4, 7, 11))   # human, dwarf, night elf, gnome, draenei


def get_vendor_items(path: str, entry: int) -> List[dict]:
    """Every item ``entry`` sells, in the order the client is shown.

    Rows whose item template is unknown are dropped here, before slots are
    numbered, because the client could not display them anyway.
    """
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            """SELECT v.item, v.maxcount, v.incrtime, v.extended_cost,
                      i.display_id, i.buy_price, i.max_durability, i.buy_count,
                      i.flags2, i.allowable_class, i.bonding
               FROM npc_vendor v
               JOIN item_templates i ON i.entry = v.item
               WHERE v.entry = ?
               ORDER BY v.slot, v.item""",
            (int(entry),),
        ).fetchall()
    except sqlite3.OperationalError:
        return []            # database predates vendors (no npc_vendor table)
    finally:
        con.close()
    return [dict(row) for row in rows[:MAX_VENDOR_ITEMS]]


def visible_to(item: dict, race: int, class_id: int) -> bool:
    """Whether a player of ``race``/``class_id`` is offered ``item``."""
    if int(item.get("extended_cost") or 0):
        return False         # honor/token prices are not implemented yet
    flags2 = int(item.get("flags2") or 0)
    alliance = int(race) in ALLIANCE_RACES
    if flags2 & ITEM_FLAG2_HORDE_ONLY and alliance:
        return False
    if flags2 & ITEM_FLAG2_ALLIANCE_ONLY and not alliance:
        return False
    allowable = int(item.get("allowable_class") or -1) & 0xFFFFFFFF
    class_mask = 1 << (max(1, int(class_id)) - 1)
    if int(item.get("bonding") or 0) == BIND_WHEN_PICKED_UP and not allowable & class_mask:
        return False
    return True


class VendorStock:
    """Limited-supply counters shared by every session.

    Keyed by creature spawn guid and item; an item with ``maxcount`` refills
    completely once ``incrtime`` seconds passed since its last refill, like
    CMaNGOS ``Creature::GetVendorItemCurrentCount``.
    """

    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._stock: Dict[Tuple[int, int], List[float]] = {}

    def remaining(self, spawn_guid: int, item: dict) -> int:
        maxcount = int(item.get("maxcount") or 0)
        if maxcount <= 0:
            return UNLIMITED
        key = (int(spawn_guid), int(item["item"]))
        now = self._clock()
        state = self._stock.get(key)
        if state is None:
            return maxcount
        count, last_refill = state
        incrtime = int(item.get("incrtime") or 0)
        if incrtime and now - last_refill >= incrtime:
            del self._stock[key]
            return maxcount
        return int(count)

    def take(self, spawn_guid: int, item: dict, amount: int) -> int:
        """Remove ``amount`` from stock; returns what is left (or UNLIMITED)."""
        left = self.remaining(spawn_guid, item)
        if left == UNLIMITED:
            return UNLIMITED
        key = (int(spawn_guid), int(item["item"]))
        new_left = max(0, left - int(amount))
        last_refill = self._stock.get(key, [0, self._clock()])[1]
        self._stock[key] = [new_left, last_refill]
        return new_left

    def clear(self) -> None:
        self._stock.clear()


STOCK = VendorStock()


def build_list_inventory(vendor_guid: int, items: List[dict], race: int,
                         class_id: int, stock_left) -> bytes:
    """``SMSG_LIST_INVENTORY``.

    uint64 vendor guid, uint8 count, then per item: uint32 vendor slot (1-based),
    uint32 item, uint32 display id, int32 left in stock (-1 = unlimited),
    uint32 price, uint32 max durability, uint32 buy count, uint32 extended cost.
    An empty list carries one extra byte (0 = "vendor has no items").
    """
    body = bytearray()
    count = 0
    for index, item in enumerate(items):
        if not visible_to(item, race, class_id):
            continue
        body += struct.pack(
            "<IIIIIIII",
            index + 1,
            int(item["item"]),
            int(item.get("display_id") or 0),
            int(stock_left(item)) & 0xFFFFFFFF,
            int(item.get("buy_price") or 0) & 0xFFFFFFFF,
            int(item.get("max_durability") or 0),
            max(1, int(item.get("buy_count") or 1)),
            int(item.get("extended_cost") or 0),
        )
        count += 1
    out = struct.pack("<QB", int(vendor_guid), count) + bytes(body)
    if count == 0:
        out += b"\x00"
    return out


def build_buy_item(vendor_guid: int, vendor_slot: int, left: int, count: int) -> bytes:
    """``SMSG_BUY_ITEM``: uint64 guid, uint32 slot (1-based), int32 left, uint32 count."""
    return struct.pack("<QIII", int(vendor_guid), int(vendor_slot) + 1,
                       int(left) & 0xFFFFFFFF, int(count))


def build_buy_failed(vendor_guid: int, item: int, error: int) -> bytes:
    """``SMSG_BUY_FAILED``: uint64 guid, uint32 item, uint8 BuyResult."""
    return struct.pack("<QIB", int(vendor_guid), int(item), int(error))


def build_sell_failed(vendor_guid: int, item_guid: int, error: int) -> bytes:
    """``SMSG_SELL_ITEM`` (only sent on failure): guid, item guid, uint8 SellResult."""
    return struct.pack("<QQB", int(vendor_guid), int(item_guid), int(error))


def parse_buy_item(data: bytes) -> Optional[Tuple[int, int, int, int]]:
    """``CMSG_BUY_ITEM``: uint64 guid, uint32 item, uint32 slot, uint32 count, uint8."""
    if len(data) < 20:
        return None
    guid, item, slot, count = struct.unpack_from("<QIII", data, 0)
    return guid, item, slot, count


def parse_buy_item_in_slot(data: bytes) -> Optional[Tuple[int, int, int, int]]:
    """``CMSG_BUY_ITEM_IN_SLOT``: guid, item, slot, uint64 bag, uint8 bag slot, uint8 count."""
    if len(data) < 26:
        return None
    guid, item, slot = struct.unpack_from("<QII", data, 0)
    count = data[25]
    return guid, item, slot, count


def parse_sell_item(data: bytes) -> Optional[Tuple[int, int, int]]:
    """``CMSG_SELL_ITEM``: uint64 vendor guid, uint64 item guid, uint32 count."""
    if len(data) < 20:
        return None
    return struct.unpack_from("<QQI", data, 0)
