"""Player talent learning, reset and client talent data."""
from __future__ import annotations

import struct
import time
from typing import Dict

from src.common.db_io import run_db_write
from src.protocol.opcodes import Opcodes
from src.world import character_db
from src.world import spells
from src.world import talents as talent_data
from src.world import trainer
from src.world import vendor


class TalentsMixin:
    # ------------------------------------------------------------------
    # State and packets
    # ------------------------------------------------------------------
    def _player_talents(self) -> dict:
        return self.character.setdefault("talents", {})

    def _spell_learn_children(self, path: str, spell_id: int) -> list[int]:
        """One talent spell plus every spell it learns through an effect."""
        spell_id = int(spell_id)
        if not spell_id:
            return []
        result = [spell_id]
        template = spells.get_spell_template(path, spell_id)
        if template is None:
            return result
        for effect in template.effects:
            if int(effect.get("effect") or 0) != \
                    spells.SPELL_EFFECT_LEARN_SPELL:
                continue
            child = int(effect.get("trigger_spell") or 0)
            if child and child not in result:
                result.append(child)
        return result

    def _talent_related_spells(self, talent, path: str) -> list[int]:
        """Rank spells plus spells learned through a talent's effects."""
        result = []
        for spell_id in talent_data.talent_rank_spells(talent):
            if not spell_id:
                continue
            result.extend(self._spell_learn_children(path, spell_id))
        return list(dict.fromkeys(result))

    @staticmethod
    def _talent_needs_effect_apply(template) -> bool:
        """Whether learning this spell must cast it like CMaNGOS ``addSpell``.

        Talent wrappers with ``SPELL_EFFECT_LEARN_SPELL`` are deliberately not
        passive, but they still have to be cast so the taught spell is learned.
        """
        if template is None:
            return False
        if template.is_passive():
            return True
        return any(int(effect.get("effect") or 0)
                   == spells.SPELL_EFFECT_LEARN_SPELL
                   for effect in template.effects)

    async def _apply_talent_spell(self, path: str, spell_id: int) -> None:
        """Cast a talent rank spell and apply the passives it teaches."""
        template = spells.get_spell_template(path, int(spell_id))
        if self._talent_needs_effect_apply(template):
            await self._apply_spell_effects(
                None, template, int(self.player_guid))
        for child in self._spell_learn_children(path, spell_id)[1:]:
            child_template = spells.get_spell_template(path, child)
            if child_template is not None and child_template.is_passive():
                await self._apply_spell_effects(
                    None, child_template, int(self.player_guid))

    async def _forget_talent_spell(self, spell_id: int,
                                   notify: bool = True) -> None:
        spell_id = int(spell_id)
        if not spell_id:
            return
        if hasattr(self, "_remove_player_aura_spell"):
            await self._remove_player_aura_spell(spell_id)
        learned = getattr(self, "known_spell_ids", None)
        if learned is not None and spell_id in learned:
            learned.discard(spell_id)
            if notify:
                await self.send_packet(
                    Opcodes.SMSG_REMOVED_SPELL,
                    trainer.build_removed_spell(spell_id))
            await run_db_write(
                character_db.forget_character_spell,
                self.config.database.world_path, int(self.player_guid),
                spell_id)

    async def _load_talents(self) -> None:
        """Restore talent ranks and make sure their spells are known."""
        if not self.character or not self.player_guid:
            return
        path = self.config.database.world_path
        stored = character_db.get_character_talents(
            path, int(self.player_guid))
        char_class = int(self.character.get("class", 0) or 0)

        # ``Player::_LoadTalents``: silently drop rows that no longer fit the
        # character's class or talent data.
        talents: Dict[int, int] = {}
        for talent_id, rank in stored.items():
            talent = talent_data.get_talent(path, int(talent_id))
            tab = (talent_data.get_talent_tab(path, int(talent["tab_id"]))
                   if talent is not None else None)
            valid = (talent is not None and tab is not None
                     and int(tab["class_mask"])
                     & talent_data.class_mask(char_class)
                     and 0 <= int(rank) < talent_data.MAX_TALENT_RANK
                     and int(talent[f"spell_rank{int(rank) + 1}"] or 0))
            if not valid:
                await run_db_write(
                    character_db.delete_character_talent,
                    path, int(self.player_guid), int(talent_id))
                continue
            talents[int(talent_id)] = int(rank)
        self.character["talents"] = talents

        learned = getattr(self, "known_spell_ids", None)
        if learned is None:
            return

        # ``UpdateFreeTalentPoints(true)``: free the tree if the character has
        # more points spent than its level allows.
        total = talent_data.talent_points_for_level(
            int(self.character.get("level", 1) or 1),
            int(self.character.get("bonus_talents", 0) or 0), char_class)
        if talent_data.used_talent_points(talents) > total:
            for talent_id in list(talents):
                talent = talent_data.get_talent(path, talent_id)
                if talent is None:
                    continue
                for related in self._talent_related_spells(talent, path):
                    await self._forget_talent_spell(
                        int(related), notify=False)
            talents.clear()
            await run_db_write(
                character_db.clear_character_talents,
                path, int(self.player_guid))

        for talent_id, rank in talents.items():
            talent = talent_data.get_talent(path, talent_id)
            if talent is None:
                continue
            spell_id = int(talent[f"spell_rank{int(rank) + 1}"] or 0)
            if not spell_id:
                continue
            # Talents that teach a spell through SPELL_EFFECT_LEARN_SPELL keep
            # the taught spell in the spellbook too.
            for related in self._spell_learn_children(path, spell_id):
                if related in learned:
                    continue
                learned.add(related)
                await run_db_write(
                    character_db.learn_character_spell,
                    path, int(self.player_guid), related, ())

    async def _apply_loaded_talents(self) -> None:
        """Reapply passive talent auras after the login burst."""
        if not self.character:
            return
        path = self.config.database.world_path
        for talent_id, rank in self._player_talents().items():
            talent = talent_data.get_talent(path, int(talent_id))
            if talent is None:
                continue
            spell_id = int(talent[f"spell_rank{int(rank) + 1}"] or 0)
            if not spell_id:
                continue
            await self._apply_talent_spell(path, spell_id)

    async def _send_talents_info(self) -> None:
        if not self.character:
            return
        payload = talent_data.build_talents_info_payload(
            self.character, self._player_talents())
        await self.send_packet(Opcodes.SMSG_TALENTS_INFO, payload)

    # ------------------------------------------------------------------
    # Validation and learning
    # ------------------------------------------------------------------
    def _talent_free_points(self) -> int:
        total = talent_data.talent_points_for_level(
            int(self.character.get("level", 1) or 1),
            int(self.character.get("bonus_talents", 0) or 0),
            int(self.character.get("class", 0) or 0))
        return max(0, total - talent_data.used_talent_points(
            self._player_talents()))

    def _reset_talent_cost(self) -> int:
        """``Player::resetTalentsCost`` in copper."""
        gold = 10000
        current = int(self.character.get("reset_talents_cost", 0) or 0)
        if current < 1 * gold:
            return 1 * gold
        if current < 5 * gold:
            return 5 * gold
        if current < 10 * gold:
            return 10 * gold
        last = int(self.character.get("reset_talents_time", 0) or 0)
        months = max(0, int((time.time() - last) // (30 * 24 * 3600)))
        if months > 0:
            return max(10 * gold, current - 5 * gold * months)
        return min(50 * gold, current + 5 * gold)

    async def handle_learn_talent(self, data: bytes) -> None:
        if not self.in_world or not self.character or len(data) < 8:
            return
        talent_id, requested_rank = struct.unpack_from("<II", data, 0)
        # ``HandleLearnTalentOpcode`` always refreshes the talent window, even
        # when the learn request was rejected.
        await self._learn_talent(
            int(talent_id), int(requested_rank), send_info=False)
        await self._send_talents_info()

    async def handle_learn_preview_talents(self, data: bytes) -> None:
        """``CMSG_LEARN_PREVIEW_TALENTS``: apply a whole preview build."""
        if not self.in_world or not self.character or len(data) < 4:
            return
        count = struct.unpack_from("<I", data, 0)[0]
        if count > 128:
            return
        offset = 4
        for _ in range(int(count)):
            if offset + 8 > len(data):
                break
            talent_id, requested_rank = struct.unpack_from(
                "<II", data, offset)
            offset += 8
            await self._learn_talent(
                int(talent_id), int(requested_rank), send_info=False)
        await self._send_talents_info()

    async def _learn_talent(self, talent_id: int, requested_rank: int,
                            send_info: bool = True) -> bool:
        path = self.config.database.world_path
        talent = talent_data.get_talent(path, talent_id)
        if talent is None:
            return False
        tab = talent_data.get_talent_tab(path, int(talent["tab_id"]))
        if tab is None:
            return False
        char_class = int(self.character.get("class", 1) or 1)
        if not (int(tab["class_mask"]) & talent_data.class_mask(char_class)):
            return False

        current = int(self._player_talents().get(talent_id, -1))
        if requested_rank <= current:
            return False
        if requested_rank >= talent_data.MAX_TALENT_RANK:
            return False
        spell_id = int(talent[f"spell_rank{requested_rank + 1}"] or 0)
        if not spell_id:
            return False
        # ``HasSpell(spellid)``: the exact rank spell is already in the
        # spellbook (for example learned from another source).
        if spell_id in getattr(self, "known_spell_ids", ()):
            return False
        cost = requested_rank - current
        if self._talent_free_points() < cost:
            return False

        # Earlier tiers need five points per tier in the same tab.
        spent = talent_data.spent_points_in_tab(
            path, int(talent["tab_id"]), self._player_talents())
        if spent < int(talent["tier_id"]) * 5:
            return False

        prereq = int(talent["prereq_talent"] or 0)
        if prereq:
            prereq_rank = int(talent["prereq_rank"] or 0)
            if int(self._player_talents().get(prereq, -1)) < prereq_rank:
                return False

        previous_spell = 0
        if current >= 0:
            previous_spell = int(
                talent[f"spell_rank{current + 1}"] or 0)
            if previous_spell and hasattr(self, "_remove_player_aura_spell"):
                await self._remove_player_aura_spell(previous_spell)
        learned = await self._learn_spell(
            spell_id, (previous_spell,) if previous_spell else ())
        if not learned and spell_id not in getattr(
                self, "known_spell_ids", ()):
            return False

        # A rank-up replaces the previous rank spell and any dependent spell
        # learned by its passive effects.  Spells belonging to the new rank are
        # kept.
        keep = set(self._spell_learn_children(path, spell_id))
        for old_spell in self._talent_related_spells(talent, path):
            if int(old_spell) not in keep:
                await self._forget_talent_spell(int(old_spell))

        self._player_talents()[talent_id] = requested_rank
        await run_db_write(
            character_db.set_character_talent,
            path, int(self.player_guid), talent_id, requested_rank)

        # Passive talents (and their rank-up effects) are applied immediately.
        await self._apply_talent_spell(path, spell_id)
        if send_info:
            await self._send_talents_info()
        return True

    async def handle_unlearn_talents(self, data: bytes) -> None:
        if not self.in_world or not self.character:
            return
        await self._reset_talents()

    def _talent_wipe_confirm_packet(self, guid: int, cost: int) -> bytes:
        return talent_data.build_talent_wipe_confirm(guid, cost)

    async def _send_talent_wipe_confirm(self, guid: int) -> None:
        """``Player::SendTalentWipeConfirm`` after the gossip option."""
        spawn = (self._trainer_spawn(int(guid))
                 if hasattr(self, "_trainer_spawn") else None)
        if spawn is None:
            return
        if not trainer.can_reset_talents(
                self.config.database.world_path, spawn, self.character):
            return
        await self.send_packet(
            Opcodes.MSG_TALENT_WIPE_CONFIRM,
            self._talent_wipe_confirm_packet(int(guid),
                                             self._reset_talent_cost()))

    async def handle_talent_wipe_confirm(self, data: bytes) -> None:
        """``MSG_TALENT_WIPE_CONFIRM``: the player accepted the respec."""
        if not self.in_world or not self.character or len(data) < 8:
            return
        guid = struct.unpack_from("<Q", data, 0)[0]
        spawn = (self._trainer_spawn(int(guid))
                 if hasattr(self, "_trainer_spawn") else None)
        if spawn is None:
            return
        if hasattr(self, "_trainer_in_range") and \
                not self._trainer_in_range(int(guid)):
            return
        if not trainer.can_reset_talents(
                self.config.database.world_path, spawn, self.character):
            return
        if not await self._reset_talents():
            # ``HandleTalentWipeConfirmOpcode``: guid 0 / cost 0 means the
            # reset was rejected (not enough money).
            await self.send_packet(
                Opcodes.MSG_TALENT_WIPE_CONFIRM,
                self._talent_wipe_confirm_packet(0, 0))
            return
        # ``unit->CastSpell(_player, 14867)`` - "Untalent Visual Effect".
        if hasattr(self, "_cast_triggered"):
            await self._cast_triggered(14867, int(self.player_guid))

    async def _reset_talents(self, no_cost: bool = False) -> bool:
        path = self.config.database.world_path
        used = talent_data.used_talent_points(self._player_talents())
        # ``Player::resetTalents``: an empty talent tree is always free.
        if used == 0:
            no_cost = True

        cost = 0
        if not no_cost:
            cost = self._reset_talent_cost()
            money = int(self.character.get("money", 0) or 0)
            if money < cost:
                await self.send_packet(
                    Opcodes.SMSG_BUY_FAILED,
                    vendor.build_buy_failed(
                        0, 0, vendor.BUY_ERR_NOT_ENOUGHT_MONEY))
                return False

        for talent_id, _rank in list(self._player_talents().items()):
            talent = talent_data.get_talent(path, int(talent_id))
            if talent is None:
                continue
            for spell_id in self._talent_related_spells(talent, path):
                await self._forget_talent_spell(int(spell_id))

        self._player_talents().clear()
        await run_db_write(
            character_db.clear_character_talents,
            path, int(self.player_guid))

        if not no_cost and cost:
            await self._modify_money(-cost)
            when = int(time.time())
            self.character["reset_talents_cost"] = cost
            self.character["reset_talents_time"] = when
            await run_db_write(
                character_db.set_reset_talents,
                path, int(self.player_guid), cost, when)

        await self._send_talents_info()
        return True
