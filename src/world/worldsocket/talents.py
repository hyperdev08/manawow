"""Player and hunter-pet talents, talent groups and respec handling."""
from __future__ import annotations

import struct
import time
from typing import Dict

from src.common.db_io import run_db_write
from src.protocol.opcodes import Opcodes
from src.world import character_db
from src.world import creature
from src.world import spells
from src.world import talents as talent_data
from src.world import trainer
from src.world import vendor

from .pets import ACT_DISABLED, ACT_PASSIVE


class TalentsMixin:
    # ------------------------------------------------------------------
    # Player talent-group state
    # ------------------------------------------------------------------
    def _talent_groups(self) -> Dict[int, Dict[int, int]]:
        groups = self.character.setdefault("talent_groups", {})
        active = int(self.character.get("active_talent_group", 0) or 0)
        groups.setdefault(active, self.character.setdefault("talents", {}))
        return groups

    def _active_talent_group(self) -> int:
        return int(self.character.get("active_talent_group", 0) or 0)

    def _player_talents(self) -> dict:
        groups = self._talent_groups()
        active = self._active_talent_group()
        if active not in groups:
            groups[active] = {}
        # ``character["talents"]`` remains an alias for the active group so the
        # rest of the server (and existing tests) can read it directly.
        self.character["talents"] = groups[active]
        return groups[active]

    def _specs_count(self) -> int:
        return max(1, min(2, int(self.character.get("specs_count", 1) or 1)))

    def _ensure_talent_groups(self) -> Dict[int, Dict[int, int]]:
        groups = self._talent_groups()
        for spec in range(self._specs_count()):
            groups.setdefault(spec, {})
        return groups

    # ------------------------------------------------------------------
    # Spell helpers
    # ------------------------------------------------------------------
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

    def _group_talent_spells(self, spec: int) -> set[int]:
        path = self.config.database.world_path
        result: set[int] = set()
        for talent_id in self._talent_groups().get(int(spec), {}):
            talent = talent_data.get_talent(path, int(talent_id))
            if talent is not None:
                result.update(self._talent_related_spells(talent, path))
        return result

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

    # ------------------------------------------------------------------
    # Login
    # ------------------------------------------------------------------
    async def _load_talents(self) -> None:
        """Restore every talent group and the active group's spells."""
        if not self.character or not self.player_guid:
            return
        path = self.config.database.world_path
        char_class = int(self.character.get("class", 0) or 0)
        specs_count = self._specs_count()
        active = self._active_talent_group()
        if active >= specs_count:
            active = 0
        self.character["active_talent_group"] = active

        stored = character_db.get_all_character_talents(
            path, int(self.player_guid))
        groups: Dict[int, Dict[int, int]] = {
            spec: {} for spec in range(specs_count)}

        for spec, rows in stored.items():
            if spec >= specs_count:
                # ``UpdateSpecCount(1)`` deletes talent data for removed specs.
                await run_db_write(
                    character_db.clear_character_talents,
                    path, int(self.player_guid), int(spec))
                continue
            for talent_id, rank in rows.items():
                talent = talent_data.get_talent(path, int(talent_id))
                tab = (talent_data.get_talent_tab(
                    path, int(talent["tab_id"]))
                    if talent is not None else None)
                valid = (talent is not None and tab is not None
                         and int(tab["class_mask"])
                         & talent_data.class_mask(char_class)
                         and 0 <= int(rank) < talent_data.MAX_TALENT_RANK
                         and int(talent[f"spell_rank{int(rank) + 1}"] or 0))
                if not valid:
                    await run_db_write(
                        character_db.delete_character_talent,
                        path, int(self.player_guid), int(talent_id),
                        int(spec))
                    continue
                groups[int(spec)][int(talent_id)] = int(rank)

        # ``UpdateFreeTalentPoints(true)``: free a group that overspent.
        total = talent_data.talent_points_for_level(
            int(self.character.get("level", 1) or 1),
            int(self.character.get("bonus_talents", 0) or 0), char_class)
        learned = getattr(self, "known_spell_ids", None)
        for spec in range(specs_count):
            if talent_data.used_talent_points(groups[spec]) <= total:
                continue
            if spec == active:
                for talent_id in list(groups[spec]):
                    talent = talent_data.get_talent(path, int(talent_id))
                    if talent is None:
                        continue
                    for related in self._talent_related_spells(talent, path):
                        await self._forget_talent_spell(
                            int(related), notify=False)
            groups[spec].clear()
            await run_db_write(
                character_db.clear_character_talents,
                path, int(self.player_guid), int(spec))

        self.character["talent_groups"] = groups
        self.character["talents"] = groups[active]

        if learned is None:
            return
        if specs_count >= 2:
            # The client casts these hidden activation spells directly.
            for spell_id in (63644, 63645, 63651):
                if spell_id in learned:
                    continue
                learned.add(spell_id)
                await run_db_write(
                    character_db.learn_character_spell,
                    path, int(self.player_guid), spell_id, ())

        # Drop stale talent spells left behind by a previous active group.
        active_spells = self._group_talent_spells(active)
        for spec in range(specs_count):
            if spec == active:
                continue
            for spell_id in self._group_talent_spells(spec) - active_spells:
                if spell_id in learned:
                    learned.discard(spell_id)
                    await run_db_write(
                        character_db.forget_character_spell,
                        path, int(self.player_guid), int(spell_id))

        # Talent spells are stored in ``character_spells`` for the active
        # group only; switching groups swaps them.
        for talent_id, rank in groups[active].items():
            talent = talent_data.get_talent(path, int(talent_id))
            if talent is None:
                continue
            spell_id = int(talent[f"spell_rank{int(rank) + 1}"] or 0)
            if not spell_id:
                continue
            for related in self._spell_learn_children(path, spell_id):
                if related in learned:
                    continue
                learned.add(related)
                await run_db_write(
                    character_db.learn_character_spell,
                    path, int(self.player_guid), related, ())

    async def _apply_loaded_talents(self) -> None:
        """Reapply passive auras for the active talent group after login."""
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

    # ------------------------------------------------------------------
    # Packets
    # ------------------------------------------------------------------
    async def _send_talents_info(self) -> None:
        if not self.character:
            return
        groups = self._ensure_talent_groups()
        payload = talent_data.build_talents_info_payload(
            self.character, groups, self._active_talent_group(),
            self._specs_count())
        await self.send_packet(Opcodes.SMSG_TALENTS_INFO, payload)

    # ------------------------------------------------------------------
    # Player validation / point maths
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

    # ------------------------------------------------------------------
    # Player learning
    # ------------------------------------------------------------------
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
        spec = self._active_talent_group()

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
            path, int(self.player_guid), talent_id, requested_rank, spec)

        # Passive talents (and their rank-up effects) are applied immediately.
        await self._apply_talent_spell(path, spell_id)
        if send_info:
            await self._send_talents_info()
        return True

    # ------------------------------------------------------------------
    # Talent-group switching
    # ------------------------------------------------------------------
    async def _purchase_dual_specialization(self, cost: int) -> bool:
        """Learn ``Learn a Second Talent Specialization`` from a trainer."""
        if self._specs_count() >= 2:
            return True
        path = self.config.database.world_path
        cost = max(0, int(cost))
        if cost and int(self.character.get("money", 0) or 0) < cost:
            await self.send_packet(
                Opcodes.SMSG_BUY_FAILED,
                vendor.build_buy_failed(
                    0, 0, vendor.BUY_ERR_NOT_ENOUGHT_MONEY))
            return False
        if cost:
            await self._modify_money(-cost)
        if 63624 not in getattr(self, "known_spell_ids", ()):
            await self._learn_spell(63624)
        template = spells.get_spell_template(path, 63624)
        if template is not None:
            await self._apply_spell_effects(
                None, template, int(self.player_guid))
        else:
            await self._update_spec_count(2)
        # 3.3.5's client uses these hidden spells for the group-switch buttons.
        for spell_id in (63644, 63645, 63651):
            if spell_id not in getattr(self, "known_spell_ids", ()):
                await self._learn_spell(spell_id)
        return True

    async def _update_spec_count(self, count: int, send_info: bool = True) -> None:
        """``Player::UpdateSpecCount`` driven by SPELL_EFFECT_TALENT_SPEC_COUNT."""
        path = self.config.database.world_path
        count = max(1, min(2, int(count)))
        current = self._specs_count()
        if count == current:
            return
        groups = self._ensure_talent_groups()
        if count < current:
            # Move off a removed group before deleting its data.
            if self._active_talent_group() >= count:
                await self._activate_talent_group(0, send_info=False)
            for spec in range(count, current):
                groups.pop(int(spec), None)
                await run_db_write(
                    character_db.clear_character_talents,
                    path, int(self.player_guid), int(spec))
        else:
            for spec in range(current, count):
                groups.setdefault(int(spec), {})
        self.character["specs_count"] = count
        await run_db_write(
            character_db.set_specs_count,
            path, int(self.player_guid), count)
        if send_info:
            await self._send_talents_info()

    async def _activate_talent_group(self, target: int,
                                     send_info: bool = True) -> bool:
        """``Player::ActivateSpec``: swap the active talent group."""
        path = self.config.database.world_path
        target = int(target)
        active = self._active_talent_group()
        if target == active or not 0 <= target < self._specs_count():
            return False
        groups = self._ensure_talent_groups()
        old_spells = self._group_talent_spells(active)
        new_spells = self._group_talent_spells(target)

        # Remove spells the new group does not use.
        for spell_id in sorted(old_spells - new_spells):
            await self._forget_talent_spell(int(spell_id), notify=True)

        self.character["active_talent_group"] = target
        self.character["talents"] = groups.setdefault(target, {})
        await run_db_write(
            character_db.set_active_talent_group,
            path, int(self.player_guid), target)

        # Learn the new group's rank spells and reapply their effects.
        for talent_id, rank in groups[target].items():
            talent = talent_data.get_talent(path, int(talent_id))
            if talent is None:
                continue
            spell_id = int(talent[f"spell_rank{int(rank) + 1}"] or 0)
            if not spell_id:
                continue
            if spell_id not in getattr(self, "known_spell_ids", ()):
                await self._learn_spell(spell_id)
            await self._apply_talent_spell(path, spell_id)

        if send_info:
            await self._send_talents_info()
        return True

    # ------------------------------------------------------------------
    # Respec
    # ------------------------------------------------------------------
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
        spec = self._active_talent_group()
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
            path, int(self.player_guid), spec)

        if not no_cost and cost:
            await self._modify_money(-cost)
            when = int(time.time())
            self.character["reset_talents_cost"] = cost
            self.character["reset_talents_time"] = when
            await run_db_write(
                character_db.set_reset_talents,
                path, int(self.player_guid), cost, when)

        # ``Player::resetTalents`` dismisses a saved hunter pet.
        await self._dismiss_hunter_pet()
        await self._send_talents_info()
        return True

    async def _dismiss_hunter_pet(self) -> None:
        if not hasattr(self, "_active_pet"):
            return
        pet = self._active_pet()
        if pet is None:
            return
        low_guid, spawn, _state = pet
        if self._pet_talent_type(spawn) >= 0 \
                and hasattr(self, "_dismiss_summon"):
            await self._dismiss_summon(int(low_guid))

    # ------------------------------------------------------------------
    # Hunter-pet talents
    # ------------------------------------------------------------------
    def _pet_talent_type(self, spawn) -> int:
        if spawn is None:
            return -1
        return talent_data.pet_talent_type(
            self.config.database.world_path, int(spawn["family"] or 0))

    def _pet_talent_tab_ok(self, spawn, tab) -> bool:
        pet_type = self._pet_talent_type(spawn)
        if pet_type < 0 or tab is None:
            return False
        return bool((1 << pet_type) & int(tab["pet_talent_mask"] or 0))

    def _pet_talents(self, state: dict) -> Dict[int, int]:
        return state.setdefault("pet_talents", {})

    def _pet_max_talent_points(self, spawn) -> int:
        level = int(spawn["min_level"] or spawn["max_level"] or 1)
        bonus = 0
        if hasattr(self, "_player_aura_mods"):
            bonus = int(self._player_aura_mods().get(
                "pet_talent_points", 0) or 0)
        return talent_data.pet_talent_points_for_level(level, bonus)

    def _pet_used_talent_points(self, state: dict) -> int:
        return talent_data.used_talent_points(self._pet_talents(state))

    def _pet_free_talent_points(self, spawn, state: dict) -> int:
        return max(0, self._pet_max_talent_points(spawn)
                   - self._pet_used_talent_points(state))

    def _pet_talent_spells(self, talent, path: str) -> list[int]:
        return self._talent_related_spells(talent, path)

    def _pet_has_spell(self, state: dict, spell_id: int) -> bool:
        return int(spell_id) in state.get("pet_spells", ())

    def _pet_add_spell(self, state: dict, spell_id: int) -> None:
        spell_id = int(spell_id)
        spells_list = state.setdefault("pet_spells", [])
        if spell_id in spells_list:
            return
        spells_list.append(spell_id)
        states = state.setdefault("pet_spell_states", {})
        if spell_id not in states:
            autocastable = (self._pet_autocastable(spell_id)
                            if hasattr(self, "_pet_autocastable") else False)
            states[spell_id] = ACT_DISABLED if autocastable else ACT_PASSIVE

    def _pet_remove_spell(self, state: dict, spell_id: int) -> None:
        spell_id = int(spell_id)
        spells_list = state.get("pet_spells") or []
        if spell_id in spells_list:
            spells_list.remove(spell_id)
        (state.get("pet_spell_states") or {}).pop(spell_id, None)
        (state.get("pet_autocast") or set()).discard(spell_id)

    def _pet_rebuild_bar(self, state: dict) -> None:
        if hasattr(self, "_pet_build_default_bar"):
            state["pet_action_bar"] = self._pet_build_default_bar(state)

    async def _apply_pet_talent_spell(self, low_guid: int, spawn,
                                      state: dict, spell_id: int,
                                      seen: set | None = None) -> None:
        """Apply one pet talent spell's aura (and taught passives) to the pet."""
        path = self.config.database.world_path
        template = spells.get_spell_template(path, int(spell_id))
        if template is None:
            return
        seen = seen if seen is not None else set()
        if int(spell_id) in seen:
            return
        seen.add(int(spell_id))
        target_guid = creature.creature_guid(
            int(low_guid), int(spawn["entry"]))
        level = int(spawn["min_level"] or spawn["max_level"] or 1)
        for index, effect in enumerate(template.effects):
            etype = int(effect.get("effect") or 0)
            if etype == spells.SPELL_EFFECT_APPLY_AURA:
                if hasattr(self, "_apply_aura_from_effect"):
                    await self._apply_aura_from_effect(
                        template, effect, index, int(self.player_guid),
                        target_guid, level, target_kind="creature")
            elif etype == spells.SPELL_EFFECT_LEARN_SPELL:
                child = int(effect.get("trigger_spell") or 0)
                if child:
                    self._pet_add_spell(state, child)
                    await self._apply_pet_talent_spell(
                        low_guid, spawn, state, child, seen)

    async def _remove_pet_talent_spell(self, low_guid: int, state: dict,
                                       spell_id: int,
                                       notify: bool = True) -> None:
        spell_id = int(spell_id)
        was_known = self._pet_has_spell(state, spell_id)
        self._pet_remove_spell(state, spell_id)
        if was_known and hasattr(self, "_remove_creature_aura_spell"):
            await self._remove_creature_aura_spell(int(low_guid), spell_id)
        if notify and was_known:
            await self.send_packet(
                Opcodes.SMSG_PET_UNLEARNED_SPELL,
                struct.pack("<I", spell_id))

    async def _load_pet_talents(self, low_guid: int, spawn,
                                state: dict) -> None:
        """Load persisted hunter-pet talents when a pet is summoned."""
        path = self.config.database.world_path
        group: Dict[int, int] = {}
        state["pet_talents"] = group
        if self._pet_talent_type(spawn) < 0:
            return
        entry = int(spawn["entry"])
        stored = character_db.get_character_pet_talents(
            path, int(self.player_guid), entry)
        pet_state = character_db.get_character_pet_state(
            path, int(self.player_guid), entry)
        if pet_state is not None:
            state["pet_reset_cost"] = int(pet_state[0] or 0)
            state["pet_reset_time"] = int(pet_state[1] or 0)
        for talent_id, rank in stored.items():
            talent = talent_data.get_talent(path, int(talent_id))
            tab = (talent_data.get_talent_tab(
                path, int(talent["tab_id"])) if talent else None)
            valid = (talent is not None and self._pet_talent_tab_ok(spawn, tab)
                     and 0 <= int(rank) < talent_data.MAX_TALENT_RANK
                     and int(talent[f"spell_rank{int(rank) + 1}"] or 0))
            if not valid:
                await run_db_write(
                    character_db.delete_character_pet_talent,
                    path, int(self.player_guid), entry, int(talent_id))
                continue
            group[int(talent_id)] = int(rank)

        if self._pet_used_talent_points(state) > self._pet_max_talent_points(
                spawn):
            group.clear()
            await run_db_write(
                character_db.clear_character_pet_talents,
                path, int(self.player_guid), entry)

        for talent_id, rank in group.items():
            talent = talent_data.get_talent(path, int(talent_id))
            if talent is None:
                continue
            spell_id = int(talent[f"spell_rank{int(rank) + 1}"] or 0)
            if not spell_id:
                continue
            for related in self._pet_talent_spells(talent, path):
                self._pet_add_spell(state, related)
            await self._apply_pet_talent_spell(
                int(low_guid), spawn, state, spell_id)

    async def _send_pet_talents_info(self, low_guid: int | None = None) -> None:
        if not self.character:
            return
        if low_guid is None:
            pet = (self._active_pet()
                   if hasattr(self, "_active_pet") else None)
        else:
            pet = (self._pet_for_guid(int(low_guid))
                   if hasattr(self, "_pet_for_guid") else None)
        if pet is None:
            return
        _low, spawn, state = pet
        if self._pet_talent_type(spawn) < 0:
            free = 0
            talents: Dict[int, int] = {}
        else:
            free = self._pet_free_talent_points(spawn, state)
            talents = dict(self._pet_talents(state))
        await self.send_packet(
            Opcodes.SMSG_TALENTS_INFO,
            talent_data.build_pet_talents_info_payload(free, talents))

    async def _learn_pet_talent(self, low_guid: int, spawn, state: dict,
                                talent_id: int, requested_rank: int,
                                notify: bool = True) -> bool:
        path = self.config.database.world_path
        talent = talent_data.get_talent(path, int(talent_id))
        if talent is None:
            return False
        tab = talent_data.get_talent_tab(path, int(talent["tab_id"]))
        if not self._pet_talent_tab_ok(spawn, tab):
            return False
        current = int(self._pet_talents(state).get(int(talent_id), -1))
        if requested_rank <= current:
            return False
        if requested_rank >= talent_data.MAX_TALENT_RANK:
            return False
        spell_id = int(talent[f"spell_rank{requested_rank + 1}"] or 0)
        if not spell_id:
            return False
        if self._pet_has_spell(state, spell_id):
            return False
        cost = requested_rank - current
        if self._pet_free_talent_points(spawn, state) < cost:
            return False

        spent = talent_data.spent_points_in_tab(
            path, int(talent["tab_id"]), self._pet_talents(state))
        if spent < int(talent["tier_id"]) * 5:
            return False
        prereq = int(talent["prereq_talent"] or 0)
        if prereq:
            if int(self._pet_talents(state).get(
                    prereq, -1)) < int(talent["prereq_rank"] or 0):
                return False

        # Replace every other rank spell of this talent (and its children).
        keep = set(self._spell_learn_children(path, spell_id))
        for old_spell in self._pet_talent_spells(talent, path):
            if int(old_spell) not in keep:
                await self._remove_pet_talent_spell(
                    low_guid, state, int(old_spell), notify=notify)
        self._pet_add_spell(state, spell_id)
        self._pet_talents(state)[int(talent_id)] = int(requested_rank)
        await run_db_write(
            character_db.set_character_pet_talent,
            path, int(self.player_guid), int(spawn["entry"]),
            int(talent_id), int(requested_rank))
        await self._apply_pet_talent_spell(
            low_guid, spawn, state, spell_id)
        if notify:
            await self.send_packet(
                Opcodes.SMSG_PET_LEARNED_SPELL,
                struct.pack("<I", spell_id))
        return True

    async def handle_pet_learn_talent(self, data: bytes) -> None:
        """``CMSG_PET_LEARN_TALENT``: spend one hunter-pet talent point."""
        if not self.in_world or not self.character or len(data) < 16:
            return
        guid, talent_id, requested_rank = struct.unpack_from("<QII", data, 0)
        pet = (self._pet_for_guid(int(guid))
               if hasattr(self, "_pet_for_guid") else None)
        if pet is None:
            return
        low_guid, spawn, state = pet
        if self._pet_talent_type(spawn) < 0:
            return
        if await self._learn_pet_talent(
                low_guid, spawn, state, int(talent_id), int(requested_rank)):
            self._pet_rebuild_bar(state)
        await self._send_pet_spells(low_guid)
        await self._send_pet_talents_info(low_guid)

    async def handle_learn_preview_talents_pet(self, data: bytes) -> None:
        """``CMSG_LEARN_PREVIEW_TALENTS_PET``: apply a whole pet build."""
        if not self.in_world or not self.character or len(data) < 12:
            return
        guid = struct.unpack_from("<Q", data, 0)[0]
        count = struct.unpack_from("<I", data, 8)[0]
        if count > 128:
            return
        pet = (self._pet_for_guid(int(guid))
               if hasattr(self, "_pet_for_guid") else None)
        if pet is None:
            return
        low_guid, spawn, state = pet
        if self._pet_talent_type(spawn) < 0:
            return
        offset = 12
        for _ in range(int(count)):
            if offset + 8 > len(data):
                break
            talent_id, requested_rank = struct.unpack_from(
                "<II", data, offset)
            offset += 8
            await self._learn_pet_talent(
                low_guid, spawn, state, int(talent_id),
                int(requested_rank), notify=False)
        self._pet_rebuild_bar(state)
        await self._send_pet_spells(low_guid)
        await self._send_pet_talents_info(low_guid)

    def _pet_reset_cost(self, state: dict) -> int:
        """``Pet::resetTalentsCost`` in copper: 10s/50s/1g, then +1g to 10g."""
        silver = 100
        gold = 10000
        days = max(0, int((time.time()
                           - int(state.get("pet_reset_time", 0) or 0))
                          // (24 * 3600)))
        current = int(state.get("pet_reset_cost", 0) or 0)
        if current < 10 * silver or days > 0:
            return 10 * silver
        if current < 50 * silver:
            return 50 * silver
        if current < 1 * gold:
            return 1 * gold
        return min(10 * gold, current + 1 * gold)

    async def _reset_pet_talents(self, low_guid: int, spawn,
                                 state: dict, no_cost: bool = False) -> bool:
        path = self.config.database.world_path
        if self._pet_used_talent_points(state) == 0:
            return False
        cost = 0
        if not no_cost:
            cost = self._pet_reset_cost(state)
            if int(self.character.get("money", 0) or 0) < cost:
                await self.send_packet(
                    Opcodes.SMSG_BUY_FAILED,
                    vendor.build_buy_failed(
                        0, 0, vendor.BUY_ERR_NOT_ENOUGHT_MONEY))
                return False

        for talent_id in list(self._pet_talents(state)):
            talent = talent_data.get_talent(path, int(talent_id))
            if talent is None:
                continue
            for spell_id in self._pet_talent_spells(talent, path):
                await self._remove_pet_talent_spell(
                    int(low_guid), state, int(spell_id))
        self._pet_talents(state).clear()
        await run_db_write(
            character_db.clear_character_pet_talents,
            path, int(self.player_guid), int(spawn["entry"]))

        if not no_cost and cost:
            await self._modify_money(-cost)
            when = int(time.time())
            state["pet_reset_cost"] = cost
            state["pet_reset_time"] = when
            await run_db_write(
                character_db.set_character_pet_reset,
                path, int(self.player_guid), int(spawn["entry"]),
                cost, when)

        self._pet_rebuild_bar(state)
        return True

    async def handle_pet_unlearn(self, data: bytes) -> None:
        """``CMSG_PET_UNLEARN``: reset the active hunter pet's talents."""
        if not self.in_world or not self.character or len(data) < 8:
            return
        guid = struct.unpack_from("<Q", data, 0)[0]
        pet = (self._pet_for_guid(int(guid))
               if hasattr(self, "_pet_for_guid") else None)
        if pet is None:
            return
        low_guid, spawn, state = pet
        if self._pet_talent_type(spawn) < 0:
            return
        await self._reset_pet_talents(low_guid, spawn, state)
        self._pet_rebuild_bar(state)
        await self._send_pet_spells(low_guid)
        await self._send_pet_talents_info(low_guid)

    async def _send_pet_unlearn_confirm(self, low_guid: int | None = None) -> None:
        pet = ((self._pet_for_guid(int(low_guid))
                if low_guid is not None else self._active_pet())
               if hasattr(self, "_pet_for_guid") else None)
        if pet is None:
            return
        low, spawn, state = pet
        if self._pet_talent_type(spawn) < 0:
            return
        await self.send_packet(
            Opcodes.SMSG_PET_UNLEARN_CONFIRM,
            struct.pack(
                "<QI",
                creature.creature_guid(int(low), int(spawn["entry"])),
                self._pet_reset_cost(state)))

    async def _abandon_pet(self, low_guid: int) -> None:
        """Abandoning a hunter pet deletes its persisted talent set too."""
        spawn = (self._summon_spawns().get(int(low_guid))
                 if hasattr(self, "_summon_spawns") else None)
        if spawn is not None and self._pet_talent_type(spawn) >= 0:
            await run_db_write(
                character_db.clear_character_pet_talents,
                self.config.database.world_path, int(self.player_guid),
                int(spawn["entry"]))
            await run_db_write(
                character_db.clear_character_pet_state,
                self.config.database.world_path, int(self.player_guid),
                int(spawn["entry"]))
        if hasattr(self, "_dismiss_summon"):
            await self._dismiss_summon(int(low_guid))
