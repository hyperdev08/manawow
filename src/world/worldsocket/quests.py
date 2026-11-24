"""Quest giver, quest log and quest objective handlers.

Rules and packet layouts live in :mod:`src.world.quests`; this mixin keeps the
per-session quest log in memory (``self._quest_log`` / ``self._quest_rewarded``),
persists every change and mirrors it into the ``PLAYER_QUEST_LOG_*`` fields.

Hooks used by other mixins (all optional through ``hasattr``):

* ``_quest_on_creature_killed`` - creature death (kill objectives);
* ``_quest_needs_item`` - loot generation (quest-only drops);
* ``_quest_items_changed`` - an item entered or left the bags.
"""
from __future__ import annotations

import struct
from typing import Dict, Iterable, List, Optional, Tuple

from src.common.db_io import run_db_write
from src.protocol.opcodes import Opcodes
from src.world import character_db
from src.world import creature
from src.world import gameobject
from src.world import quests as Q
from src.world import update_fields as F

# How close a quest object (wanted poster, quest book) must be to talk to it.
QUEST_OBJECT_DISTANCE = 10.0

MAX_PLAYER_LEVEL = 80
EQUIP_ERR_INV_FULL = 50


class QuestsMixin:
    # ------------------------------------------------------------------
    # State
    # ------------------------------------------------------------------

    def _load_quest_state(self) -> None:
        """Read the quest log at login and expose its update fields."""
        path = self.config.database.world_path
        self._quest_log: Dict[int, dict] = Q.load_quest_log(path, int(self.player_guid))
        self._quest_rewarded = Q.load_rewarded_quests(path, int(self.player_guid))
        if self.character is not None:
            self.character["quest_fields"] = Q.quest_log_fields(self._quest_log)

    def _quest_log_map(self) -> Dict[int, dict]:
        if not hasattr(self, "_quest_log"):
            self._quest_log = {}
            self._quest_rewarded = set()
        return self._quest_log

    def _quest_state(self) -> Q.QuestState:
        char = self.character or {}
        return Q.QuestState(
            int(char.get("race", 1)), int(char.get("class", 1)),
            int(char.get("level", 1)), self._quest_log_map(),
            getattr(self, "_quest_rewarded", set()),
        )

    def _quest_item_counts(self) -> Dict[int, int]:
        counts: Dict[int, int] = {}
        for item in (self.character or {}).get("inventory", {}).values():
            entry = int(item["item_entry"])
            counts[entry] = counts.get(entry, 0) + max(1, int(item["stack_count"] or 1))
        return counts

    async def _send_quest_slot(self, quest_id: int, entry: Optional[dict],
                               slot: Optional[int] = None) -> None:
        slot = int(entry["slot"]) if entry is not None else int(slot)
        fields = Q.slot_fields(slot, quest_id, entry)
        if self.character is not None:
            stored = self.character.setdefault("quest_fields", {})
            stored.update(fields)
        if hasattr(self, "_send_player_fields"):
            await self._send_player_fields(fields)
        # Quest chests start or stop glittering for this player.
        if hasattr(self, "refresh_gameobject_quest_flags"):
            await self.refresh_gameobject_quest_flags()

    async def _save_quest(self, quest_id: int, entry: dict) -> None:
        await run_db_write(
            Q.save_quest, self.config.database.world_path,
            int(self.player_guid), quest_id, entry)

    # ------------------------------------------------------------------
    # Quest giver lookup
    # ------------------------------------------------------------------

    @staticmethod
    def _giver_kind(spawn) -> str:
        """``creature`` for NPC rows; objects and items carry ``giver_kind``."""
        return spawn["giver_kind"] if "giver_kind" in spawn.keys() else Q.GIVER_CREATURE

    def _quest_item_giver(self, guid: int):
        """An item in the bags that starts a quest (``item_template.startquest``)."""
        for item in (self.character or {}).get("inventory", {}).values():
            if int(item.get("guid") or 0) != int(guid):
                continue
            entry = int(item["item_entry"])
            if not Q.quests_started_by(self.config.database.world_path, entry, Q.GIVER_ITEM):
                return None
            return {"entry": entry, "giver_kind": Q.GIVER_ITEM, "guid": int(guid)}
        return None

    def _questgiver_spawn(self, guid: int):
        """The quest giver behind ``guid`` (creature, object or item), or ``None``."""
        if not guid or self._is_player_guid(guid):
            return None
        if (int(guid) >> 48) == (F.ITEM_HIGH_GUID >> 48):
            return self._quest_item_giver(guid)
        if gameobject.is_gameobject_guid(guid):
            spawn = (self._usable_gameobject(guid, max_distance=QUEST_OBJECT_DISTANCE)
                     if hasattr(self, "_usable_gameobject") else None)
            if spawn is None or not Q.is_quest_giver(
                    self.config.database.world_path, int(spawn["entry"]), Q.GIVER_GAMEOBJECT):
                return None
            return dict(spawn, giver_kind=Q.GIVER_GAMEOBJECT)
        low_guid = creature.creature_low_guid(guid)
        spawn = self._get_creature_spawn(low_guid)
        if spawn is None:
            return None
        if not int(spawn["npc_flags"]) & Q.UNIT_NPC_FLAG_QUESTGIVER:
            return None
        return spawn

    def _questgiver_status(self, entry: int, kind: str = Q.GIVER_CREATURE) -> int:
        return Q.giver_status(self.config.database.world_path, int(entry),
                              self._quest_state(), kind)

    async def handle_questgiver_status_query(self, data: bytes):
        """``CMSG_QUESTGIVER_STATUS_QUERY``: uint64 guid."""
        if not self.in_world or len(data) < 8:
            return
        guid = struct.unpack_from("<Q", data, 0)[0]
        spawn = self._questgiver_spawn(guid)
        status = (self._questgiver_status(int(spawn["entry"]), self._giver_kind(spawn))
                  if spawn else Q.DIALOG_STATUS_NONE)
        await self.send_packet(Opcodes.SMSG_QUESTGIVER_STATUS,
                               Q.build_questgiver_status(guid, status))

    async def handle_questgiver_status_multiple_query(self, data: bytes = b""):
        """``CMSG_QUESTGIVER_STATUS_MULTIPLE_QUERY``: every visible giver."""
        if not self.in_world:
            return
        await self._send_visible_questgiver_statuses()

    async def _send_visible_questgiver_statuses(self) -> None:
        statuses: List[Tuple[int, int]] = []
        for low_guid, object_guid in list(getattr(self, "_visible_creatures", {}).items()):
            spawn = self._get_creature_spawn(int(low_guid))
            if spawn is None or not int(spawn["npc_flags"]) & Q.UNIT_NPC_FLAG_QUESTGIVER:
                continue
            statuses.append((int(object_guid), self._questgiver_status(int(spawn["entry"]))))
        path = self.config.database.world_path
        for low_guid, spawn in list(getattr(self, "_gameobject_spawn_cache", {}).items()):
            if int(spawn["type"]) != gameobject.GAMEOBJECT_TYPE_QUESTGIVER:
                continue
            if not Q.is_quest_giver(path, int(spawn["entry"]), Q.GIVER_GAMEOBJECT):
                continue
            statuses.append((int(self._visible_gameobjects[low_guid]),
                             self._questgiver_status(int(spawn["entry"]), Q.GIVER_GAMEOBJECT)))
        await self.send_packet(Opcodes.SMSG_QUESTGIVER_STATUS_MULTIPLE,
                               Q.build_questgiver_status_multiple(statuses))

    # ------------------------------------------------------------------
    # Opening a quest giver
    # ------------------------------------------------------------------

    def _quest_menu(self, entry: int, kind: str = Q.GIVER_CREATURE) -> List[Tuple[int, int]]:
        return Q.quest_menu(self.config.database.world_path, int(entry),
                            self._quest_state(), kind)

    def _gossip_quest_items(self, spawn) -> List[dict]:
        """Quest rows to append to a gossip window (empty when not a giver)."""
        if spawn is None or not int(spawn["npc_flags"]) & Q.UNIT_NPC_FLAG_QUESTGIVER:
            return []
        level = int((self.character or {}).get("level", 1))
        return Q.gossip_quest_items(self.config.database.world_path,
                                    self._quest_menu(int(spawn["entry"])), level)

    async def _send_quest_menu(self, guid: int, spawn, greeting: str = "") -> bool:
        """``Player::SendPreparedQuest``: one quest opens directly, more list.

        Returns ``False`` when the creature has nothing to offer.
        """
        path = self.config.database.world_path
        menu = self._quest_menu(int(spawn["entry"]), self._giver_kind(spawn))
        if not menu:
            return False
        if len(menu) == 1:
            quest_id, icon = menu[0]
            quest = Q.get_quest_template(path, quest_id)
            if icon == Q.QUEST_ICON_AVAILABLE:
                await self._send_quest_details(guid, quest)
            else:
                await self._send_request_items(guid, quest, close_on_cancel=True)
            return True
        level = int(self.character.get("level", 1))
        rows = [(Q.get_quest_template(path, q), icon) for q, icon in menu]
        await self.send_packet(
            Opcodes.SMSG_QUESTGIVER_QUEST_LIST,
            Q.build_quest_list(guid, greeting, rows, level),
        )
        return True

    async def handle_questgiver_hello(self, data: bytes):
        """``CMSG_QUESTGIVER_HELLO``: right-click on a quest-only NPC."""
        if not self.in_world or len(data) < 8:
            return
        guid = struct.unpack_from("<Q", data, 0)[0]
        spawn = self._questgiver_spawn(guid)
        if spawn is None:
            return
        if not await self._send_quest_menu(guid, spawn):
            await self.send_packet(Opcodes.SMSG_GOSSIP_COMPLETE, b"")

    async def _send_quest_details(self, guid: int, quest) -> None:
        path = self.config.database.world_path
        level = int(self.character.get("level", 1))
        displays = Q.item_display_ids(
            path, [i for i, _ in Q.reward_choices(quest) + Q.reward_items(quest)])
        await self.send_packet(
            Opcodes.SMSG_QUESTGIVER_QUEST_DETAILS,
            Q.build_quest_details(guid, quest, level, displays),
        )

    async def _send_request_items(self, guid: int, quest, close_on_cancel: bool = False) -> None:
        """Show "bring me" text, or skip straight to the reward window.

        ``PlayerMenu::SendQuestGiverRequestItems``: a quest without item
        objectives that can be completed goes directly to OfferReward.
        """
        can_complete = self._quest_can_reward(quest)
        if can_complete and not Q.item_objectives(quest):
            await self._send_offer_reward(guid, quest)
            return
        displays = Q.item_display_ids(
            self.config.database.world_path, [i for i, _ in Q.item_objectives(quest)])
        await self.send_packet(
            Opcodes.SMSG_QUESTGIVER_REQUEST_ITEMS,
            Q.build_request_items(guid, quest, can_complete, displays, close_on_cancel),
        )

    async def _send_offer_reward(self, guid: int, quest) -> None:
        level = int(self.character.get("level", 1))
        displays = Q.item_display_ids(
            self.config.database.world_path,
            [i for i, _ in Q.reward_choices(quest) + Q.reward_items(quest)])
        await self.send_packet(
            Opcodes.SMSG_QUESTGIVER_OFFER_REWARD,
            Q.build_offer_reward(guid, quest, level, displays),
        )

    def _giver_has_quest(self, spawn, quest_id: int, ender: bool) -> bool:
        path = self.config.database.world_path
        relation = Q.quests_ended_by if ender else Q.quests_started_by
        return int(quest_id) in relation(path, int(spawn["entry"]), self._giver_kind(spawn))

    async def handle_questgiver_query_quest(self, data: bytes):
        """``CMSG_QUESTGIVER_QUERY_QUEST``: guid, quest id, uint8 unk."""
        if not self.in_world or len(data) < 12:
            return
        guid, quest_id = struct.unpack_from("<QI", data, 0)
        spawn = self._questgiver_spawn(guid)
        quest = Q.get_quest_template(self.config.database.world_path, quest_id)
        if spawn is None or quest is None:
            return
        if quest_id in self._quest_log_map() and self._giver_has_quest(spawn, quest_id, True):
            await self._send_request_items(guid, quest, close_on_cancel=True)
        elif self._giver_has_quest(spawn, quest_id, False):
            await self._send_quest_details(guid, quest)

    async def handle_quest_query(self, data: bytes):
        """``CMSG_QUEST_QUERY``: the client caches quest text and objectives."""
        if len(data) < 4:
            return
        quest_id = struct.unpack_from("<I", data, 0)[0]
        quest = Q.get_quest_template(self.config.database.world_path, quest_id)
        if quest is None:
            self.log.debug(f"Quest query for unknown quest {quest_id}")
            return
        await self.send_packet(Opcodes.SMSG_QUEST_QUERY_RESPONSE,
                               Q.build_quest_query_response(quest))

    async def handle_quest_poi_query(self, data: bytes):
        """``CMSG_QUEST_POI_QUERY``: uint32 count + ids; answer with no POIs."""
        if len(data) < 4:
            return
        count = min(struct.unpack_from("<I", data, 0)[0], Q.MAX_QUEST_LOG_SIZE)
        ids = [struct.unpack_from("<I", data, 4 + 4 * i)[0]
               for i in range(count) if 8 + 4 * i <= len(data)]
        await self.send_packet(Opcodes.SMSG_QUEST_POI_QUERY_RESPONSE,
                               Q.build_quest_poi_response(ids))

    async def handle_questgiver_cancel(self, data: bytes = b""):
        await self.send_packet(Opcodes.SMSG_GOSSIP_COMPLETE, b"")

    # ------------------------------------------------------------------
    # Accept / abandon
    # ------------------------------------------------------------------

    async def _send_quest_invalid(self, reason: int) -> None:
        await self.send_packet(Opcodes.SMSG_QUESTGIVER_QUEST_INVALID,
                               Q.build_quest_invalid(reason))

    async def handle_questgiver_accept_quest(self, data: bytes):
        """``CMSG_QUESTGIVER_ACCEPT_QUEST``: guid, quest id, uint32 unk."""
        if not self.in_world or len(data) < 12:
            return
        guid, quest_id = struct.unpack_from("<QI", data, 0)
        path = self.config.database.world_path
        spawn = self._questgiver_spawn(guid)
        quest = Q.get_quest_template(path, quest_id)
        if spawn is None or quest is None or not self._giver_has_quest(spawn, quest_id, False):
            await self.send_packet(Opcodes.SMSG_GOSSIP_COMPLETE, b"")
            return
        accepted = await self._accept_quest(quest)
        if accepted and self._giver_kind(spawn) == Q.GIVER_ITEM:
            # Consume the starting item before the client is told the gossip is
            # complete, so the persisted inventory is already consistent when
            # the next request (or a relog) arrives.
            await self._consume_quest_starter(spawn, quest)
        await self.send_packet(Opcodes.SMSG_GOSSIP_COMPLETE, b"")
        if self._giver_kind(spawn) == Q.GIVER_ITEM:
            return
        await self.send_packet(
            Opcodes.SMSG_QUESTGIVER_STATUS,
            Q.build_questgiver_status(
                guid, self._questgiver_status(int(spawn["entry"]), self._giver_kind(spawn))),
        )

    async def _consume_quest_starter(self, giver, quest) -> None:
        """``Player::AddQuestAndCheckCompletion``: the starting item is used up,
        unless the quest itself asks for it."""
        entry = int(giver["entry"])
        if any(item == entry for item, _count in Q.item_objectives(quest)):
            return
        for slot, item in list(self.character.get("inventory", {}).items()):
            if int(item.get("guid") or 0) == int(giver["guid"]):
                await self._remove_item_from_slot(int(slot), 1, notify_quests=False)
                return

    async def handle_use_item(self, data: bytes):
        """``CMSG_USE_ITEM``: only quest-starting items are handled here.

        ``uint8 bag, uint8 slot, uint8 castCount, uint32 spellId,
        uint64 itemGuid, ...``.  Items with an on-use spell (potions, food,
        hearthstone) are item spell casting, part of #11.
        """
        if not self.in_world or not self.character or len(data) < 15:
            return
        item_guid = struct.unpack_from("<Q", data, 7)[0]
        giver = self._quest_item_giver(item_guid)
        if giver is None:
            self.log.debug(f"Item use for {item_guid:#x}: only quest items are handled")
            return
        path = self.config.database.world_path
        quest_id = Q.quests_started_by(path, giver["entry"], Q.GIVER_ITEM)[0]
        quest = Q.get_quest_template(path, quest_id)
        if quest is None:
            return
        state = self._quest_state()
        if quest_id in self._quest_log_map():
            await self._send_quest_invalid(Q.INVALIDREASON_QUEST_ALREADY_ON)
        elif quest_id in state.rewarded and not Q.is_repeatable(quest):
            await self._send_quest_invalid(Q.INVALIDREASON_QUEST_ALREADY_DONE)
        else:
            await self._send_quest_details(item_guid, quest)

    async def _accept_quest(self, quest) -> bool:
        """``Player::AddQuest``; returns ``True`` when the quest was added."""
        path = self.config.database.world_path
        quest_id = int(quest["entry"])
        log = self._quest_log_map()
        state = self._quest_state()
        if quest_id in log:
            await self._send_quest_invalid(Q.INVALIDREASON_QUEST_ALREADY_ON)
            return False
        if quest_id in state.rewarded and not Q.is_repeatable(quest):
            await self._send_quest_invalid(Q.INVALIDREASON_QUEST_ALREADY_DONE)
            return False
        if not Q.satisfies_race_class(quest, state.race, state.char_class):
            await self._send_quest_invalid(Q.INVALIDREASON_QUEST_FAILED_WRONG_RACE)
            return False
        if not Q.satisfies_level(quest, state.level):
            await self._send_quest_invalid(Q.INVALIDREASON_QUEST_FAILED_LOW_LEVEL)
            return False
        if not Q.can_take_quest(path, quest, state):
            await self._send_quest_invalid(Q.INVALIDREASON_DONT_HAVE_REQ)
            return False
        slot = Q.free_log_slot(log)
        if slot < 0:
            await self.send_packet(Opcodes.SMSG_QUESTLOG_FULL, b"")
            return False
        src_item = int(quest.get("src_item_id") or 0)
        if src_item and not await self._quest_give_item(
                src_item, max(1, int(quest.get("src_item_count") or 1))):
            return False

        entry = Q.new_log_entry(slot)
        log[quest_id] = entry
        if Q.objectives_complete(quest, entry, self._quest_item_counts()):
            entry["status"] = Q.QUEST_STATUS_COMPLETE
        await self._save_quest(quest_id, entry)
        await self._send_quest_slot(quest_id, entry)
        if entry["status"] == Q.QUEST_STATUS_COMPLETE:
            await self.send_packet(Opcodes.SMSG_QUESTUPDATE_COMPLETE,
                                   Q.build_questupdate_complete(quest_id))
        self.log.info(f"'{self.character.get('name')}' accepted quest {quest_id} "
                      f"'{quest.get('title')}' (slot {slot})")
        return True

    async def handle_questlog_remove_quest(self, data: bytes):
        """``CMSG_QUESTLOG_REMOVE_QUEST``: uint8 quest-log slot."""
        if not self.in_world or len(data) < 1:
            return
        slot = data[0]
        log = self._quest_log_map()
        quest_id = next((q for q, e in log.items() if int(e["slot"]) == slot), 0)
        if not quest_id:
            return
        del log[quest_id]
        await run_db_write(
            Q.delete_quest, self.config.database.world_path,
            int(self.player_guid), quest_id)
        await self._send_quest_slot(quest_id, None, slot=slot)
        quest = Q.get_quest_template(self.config.database.world_path, quest_id)
        if quest is not None and int(quest.get("src_item_id") or 0):
            # ``Player::TakeQuestSourceItem``: the starting item goes too.
            await self._quest_take_items(int(quest["src_item_id"]),
                                         max(1, int(quest.get("src_item_count") or 1)))
        self.log.info(f"'{self.character.get('name')}' abandoned quest {quest_id}")
        await self._send_visible_questgiver_statuses()

    # ------------------------------------------------------------------
    # Turn in
    # ------------------------------------------------------------------

    def _quest_can_reward(self, quest) -> bool:
        """``Player::CanRewardQuest``: complete, items in bags, money in purse."""
        entry = self._quest_log_map().get(int(quest["entry"]))
        if entry is None or int(entry["status"]) != Q.QUEST_STATUS_COMPLETE:
            return False
        if not Q.objectives_complete(quest, entry, self._quest_item_counts()):
            return False
        return int(self.character.get("money", 0)) >= Q.required_money(quest)

    async def handle_questgiver_complete_quest(self, data: bytes):
        """``CMSG_QUESTGIVER_COMPLETE_QUEST``: guid, quest id."""
        if not self.in_world or len(data) < 12:
            return
        guid, quest_id = struct.unpack_from("<QI", data, 0)
        spawn = self._questgiver_spawn(guid)
        quest = Q.get_quest_template(self.config.database.world_path, quest_id)
        if (spawn is None or quest is None or quest_id not in self._quest_log_map()
                or not self._giver_has_quest(spawn, quest_id, True)):
            return
        await self._send_request_items(guid, quest)

    async def handle_questgiver_request_reward(self, data: bytes):
        """``CMSG_QUESTGIVER_REQUEST_REWARD``: "Continue" in RequestItems."""
        if not self.in_world or len(data) < 12:
            return
        guid, quest_id = struct.unpack_from("<QI", data, 0)
        spawn = self._questgiver_spawn(guid)
        quest = Q.get_quest_template(self.config.database.world_path, quest_id)
        if spawn is None or quest is None or not self._giver_has_quest(spawn, quest_id, True):
            return
        if self._quest_can_reward(quest):
            await self._send_offer_reward(guid, quest)

    async def handle_questgiver_choose_reward(self, data: bytes):
        """``CMSG_QUESTGIVER_CHOOSE_REWARD``: guid, quest id, reward index."""
        if not self.in_world or len(data) < 16:
            return
        guid, quest_id, choice = struct.unpack_from("<QII", data, 0)
        path = self.config.database.world_path
        spawn = self._questgiver_spawn(guid)
        quest = Q.get_quest_template(path, quest_id)
        if spawn is None or quest is None or not self._giver_has_quest(spawn, quest_id, True):
            return
        if not await self._reward_quest(quest, choice):
            return
        # ``HandleQuestgiverChooseRewardOpcode``: offer the next quest of the
        # chain right away when the same NPC starts it.
        next_id = int(quest.get("next_quest_in_chain") or 0)
        next_quest = Q.get_quest_template(path, next_id) if next_id else None
        if (next_quest is not None and self._giver_has_quest(spawn, next_id, False)
                and Q.can_take_quest(path, next_quest, self._quest_state())):
            await self._send_quest_details(guid, next_quest)
        else:
            await self.send_packet(Opcodes.SMSG_GOSSIP_COMPLETE, b"")
        await self._send_visible_questgiver_statuses()

    async def _reward_quest(self, quest, choice: int) -> bool:
        """``Player::RewardQuest``: take objectives, hand out the rewards."""
        path = self.config.database.world_path
        quest_id = int(quest["entry"])
        if not self._quest_can_reward(quest):
            return False
        choices = Q.reward_choices(quest)
        rewards = list(Q.reward_items(quest))
        if choices:
            if choice >= len(choices):
                return False
            rewards.append(choices[choice])
        if rewards and self._free_backpack_slots() < len(rewards):
            await self._send_inventory_failure(EQUIP_ERR_INV_FULL)
            return False

        for item, count in Q.item_objectives(quest):
            await self._quest_take_items(item, count)
        if Q.required_money(quest):
            await self._modify_money(-Q.required_money(quest))
        for item, count in rewards:
            await self._quest_give_item(item, count)

        level = int(self.character.get("level", 1))
        xp = Q.quest_xp(quest, level) if level < MAX_PLAYER_LEVEL else 0
        money = Q.reward_money(quest)
        if level >= MAX_PLAYER_LEVEL:
            money += int(quest.get("rew_money_max_level") or 0)

        entry = self._quest_log_map().pop(quest_id)
        self._quest_rewarded.add(quest_id)
        await run_db_write(
            Q.reward_quest, path, int(self.player_guid), quest_id)
        await self._send_quest_slot(quest_id, None, slot=int(entry["slot"]))

        if money:
            await self._modify_money(money)
        await self.send_packet(
            Opcodes.SMSG_QUESTGIVER_QUEST_COMPLETE,
            Q.build_quest_complete(quest_id, xp, money,
                                   int(quest.get("rew_honor_addition") or 0),
                                   int(quest.get("bonus_talents") or 0)),
        )
        if xp:
            await self._award_xp(xp, 0)
        if int(quest.get("rew_spell_cast") or 0) or int(quest.get("rew_spell") or 0):
            self.log.debug(f"Quest {quest_id} reward spell not applied (not supported)")
        self.log.info(f"'{self.character.get('name')}' completed quest {quest_id} "
                      f"'{quest.get('title')}': {xp} XP, {money} copper, "
                      f"items {rewards}")
        return True

    # ------------------------------------------------------------------
    # Objectives
    # ------------------------------------------------------------------

    async def _quest_on_creature_killed(self, guid: int, entry: int) -> None:
        """``Player::KilledMonsterCredit`` for every matching kill objective."""
        log = self._quest_log_map()
        if not log:
            return
        path = self.config.database.world_path
        credits = {int(entry)}
        template = creature.get_creature_template(path, int(entry))
        if template is not None:
            for column in ("kill_credit1", "kill_credit2"):
                if column in template.keys() and int(template[column] or 0):
                    credits.add(int(template[column]))
        changed = False
        for quest_id, state in list(log.items()):
            if int(state["status"]) != Q.QUEST_STATUS_INCOMPLETE:
                continue
            quest = Q.get_quest_template(path, quest_id)
            if quest is None:
                continue
            updated = False
            for index, target, required in Q.kill_objectives(quest):
                if target <= 0 or target not in credits:
                    continue
                current = int(state["counts"][index])
                if current >= required:
                    continue
                state["counts"][index] = current + 1
                updated = True
                await self.send_packet(
                    Opcodes.SMSG_QUESTUPDATE_ADD_KILL,
                    Q.build_questupdate_add_kill(quest_id, target, current + 1,
                                                 required, guid),
                )
            if updated:
                await self._quest_check_complete(quest_id, quest, state)
                changed = True
        if changed:
            await self._send_visible_questgiver_statuses()

    async def _quest_on_gameobject_used(self, guid: int, entry: int) -> None:
        """``Player::KillCreditGO``: "use the object" objectives (negative ids)."""
        log = self._quest_log_map()
        if not log:
            return
        path = self.config.database.world_path
        changed = False
        for quest_id, state in list(log.items()):
            if int(state["status"]) != Q.QUEST_STATUS_INCOMPLETE:
                continue
            quest = Q.get_quest_template(path, quest_id)
            if quest is None:
                continue
            updated = False
            for index, go_entry, required in Q.object_objectives(quest):
                if go_entry != int(entry):
                    continue
                current = int(state["counts"][index])
                if current >= required:
                    continue
                state["counts"][index] = current + 1
                updated = True
                await self.send_packet(
                    Opcodes.SMSG_QUESTUPDATE_ADD_KILL,
                    Q.build_questupdate_add_kill(quest_id, -go_entry, current + 1,
                                                 required, guid),
                )
            if updated:
                await self._quest_check_complete(quest_id, quest, state)
                changed = True
        if changed:
            await self._send_visible_questgiver_statuses()

    async def _quest_check_complete(self, quest_id: int, quest, state: dict) -> None:
        """Re-evaluate completion after a counter/item/explore change."""
        complete = Q.objectives_complete(quest, state, self._quest_item_counts())
        was_complete = int(state["status"]) == Q.QUEST_STATUS_COMPLETE
        if complete and not was_complete:
            state["status"] = Q.QUEST_STATUS_COMPLETE
        elif not complete and was_complete:
            state["status"] = Q.QUEST_STATUS_INCOMPLETE
        await self._save_quest(quest_id, state)
        await self._send_quest_slot(quest_id, state)
        if complete and not was_complete:
            await self.send_packet(Opcodes.SMSG_QUESTUPDATE_COMPLETE,
                                   Q.build_questupdate_complete(quest_id))

    def _quest_needs_item(self, item_entry: int) -> bool:
        """True when an active quest still needs more of ``item_entry``."""
        path = self.config.database.world_path
        counts = None
        for quest_id, state in self._quest_log_map().items():
            if int(state["status"]) != Q.QUEST_STATUS_INCOMPLETE:
                continue
            quest = Q.get_quest_template(path, quest_id)
            if quest is None:
                continue
            for item, required in Q.item_objectives(quest):
                if item != int(item_entry):
                    continue
                if counts is None:
                    counts = self._quest_item_counts()
                if counts.get(item, 0) < required:
                    return True
        return False

    async def _quest_items_changed(self, item_entries: Iterable[int]) -> None:
        """An item was added or removed: refresh the quests that need it."""
        entries = {int(e) for e in item_entries}
        if not entries:
            return
        path = self.config.database.world_path
        changed = False
        for quest_id, state in list(self._quest_log_map().items()):
            quest = Q.get_quest_template(path, quest_id)
            if quest is None:
                continue
            if not any(item in entries for item, _ in Q.item_objectives(quest)):
                continue
            before = int(state["status"])
            await self._quest_check_complete(quest_id, quest, state)
            changed = changed or before != int(state["status"])
        if changed:
            await self._send_visible_questgiver_statuses()

    async def handle_area_trigger(self, data: bytes):
        """``CMSG_AREATRIGGER``: uint32 AreaTrigger.dbc id (exploration)."""
        if not self.in_world or len(data) < 4:
            return
        trigger_id = struct.unpack_from("<I", data, 0)[0]
        path = self.config.database.world_path
        quest_id = Q.quest_for_areatrigger(path, trigger_id)
        state = self._quest_log_map().get(quest_id) if quest_id else None
        if state is None or int(state["explored"]):
            return
        quest = Q.get_quest_template(path, quest_id)
        if quest is None:
            return
        state["explored"] = 1
        await self._quest_check_complete(quest_id, quest, state)
        self.log.info(f"Area trigger {trigger_id} explored for quest {quest_id}")

    # ------------------------------------------------------------------
    # Items and money
    # ------------------------------------------------------------------

    def _free_backpack_slots(self) -> int:
        used = set((self.character or {}).get("inventory", {}).keys())
        return sum(1 for slot in range(256, 272) if slot not in used)

    async def _quest_give_item(self, item_entry: int, count: int) -> bool:
        """Put ``count`` of an item in the backpack and tell the client."""
        changes = await run_db_write(
            character_db.add_item_to_inventory,
            self.config.database.world_path, int(self.player_guid),
            int(item_entry), int(count),
        )
        if not changes:
            await self._send_inventory_failure(EQUIP_ERR_INV_FULL)
            return False
        # Shows the items and re-evaluates collect objectives.
        await self._deliver_item_changes(int(item_entry), changes)
        await self.broadcast_equipment_update()
        return True

    async def _quest_take_items(self, item_entry: int, count: int) -> int:
        """Remove up to ``count`` of an item from the bags; returns removed.

        Used while handing a quest in or abandoning it, so the quest log is
        not re-evaluated here (it would flag the quest incomplete mid turn-in).
        """
        inventory = (self.character or {}).get("inventory", {})
        remaining = int(count)
        removed = 0
        for slot in sorted(inventory):
            if remaining <= 0:
                break
            item = inventory[slot]
            if int(item["item_entry"]) != int(item_entry):
                continue
            stack = max(1, int(item["stack_count"] or 1))
            take = await self._remove_item_from_slot(
                slot, min(stack, remaining), notify_quests=False)
            remaining -= take
            removed += take
        return removed
