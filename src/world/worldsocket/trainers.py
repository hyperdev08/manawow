"""NPC spell trainers: list, prerequisites and buying spells."""
from __future__ import annotations

import random
import struct

from src.common.db_io import run_db_write
from src.protocol.opcodes import Opcodes
from src.world import character_db
from src.world import creature
from src.world import object_update
from src.world import spells
from src.world import trainer


class TrainersMixin:
    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _trainer_spawn(self, guid: int):
        """The spawn behind ``guid`` when it carries the trainer NPC flag."""
        low_guid = creature.creature_low_guid(guid)
        spawn = self._get_creature_spawn(low_guid)
        if spawn is None or not (int(spawn["npc_flags"] or 0)
                                 & trainer.NPC_FLAG_TRAINER):
            return None
        return spawn

    def _trainer_in_range(self, guid: int) -> bool:
        low_guid = creature.creature_low_guid(guid)
        return self._distance(
            self._player_position(), self._creature_position(low_guid)
        ) <= trainer.TRAINER_INTERACT_DISTANCE

    def _known_spells(self) -> set[int]:
        return set(int(spell_id)
                   for spell_id in getattr(self, "known_spell_ids", ()))

    def _player_skills(self) -> dict:
        return self.character.get("skills", {}) if self.character else {}

    def _can_use_trainer(self, spawn) -> bool:
        if not self.character:
            return False
        path = self.config.database.world_path
        return trainer.is_trainer_of(
            path, spawn, self.character, self._known_spells())

    async def _send_trainer_list(self, guid: int, spawn) -> None:
        path = self.config.database.world_path
        payload, count = trainer.build_trainer_list(
            path, guid, spawn, self.character, self._known_spells(),
            self._player_skills())
        if not count:
            self.log.debug(
                f"Trainer '{spawn['name']}' ({guid}) has no spells for "
                f"'{self.character.get('name', '?')}'")
            return
        await self.send_packet(Opcodes.SMSG_TRAINER_LIST, payload)
        self.log.info(
            f"Trainer list for '{spawn['name']}' ({guid}): {count} spell(s)")

    async def _learn_spell(self, spell_id: int, superseded=()) -> bool:
        """Add one spell to the spellbook and tell the client."""
        spell_id = int(spell_id)
        if spell_id in self.known_spell_ids:
            return False
        for old_spell in superseded:
            if int(old_spell) in self.known_spell_ids:
                self.known_spell_ids.discard(int(old_spell))
                await self.send_packet(
                    Opcodes.SMSG_SUPERCEDED_SPELL,
                    trainer.build_superceded_spell(int(old_spell), spell_id),
                )
        self.known_spell_ids.add(spell_id)
        await self.send_packet(
            Opcodes.SMSG_LEARNED_SPELL, trainer.build_learned_spell(spell_id))
        await run_db_write(
            character_db.learn_character_spell,
            self.config.database.world_path, int(self.player_guid), spell_id,
            tuple(int(old) for old in superseded),
        )
        return True

    async def _apply_trainer_skill(self, template) -> None:
        """Apply ``SPELL_EFFECT_SKILL_STEP`` from a profession/riding wrapper."""
        skill_id, step = trainer.skill_step_from_template(template)
        if not skill_id or not step:
            # ``SPELL_EFFECT_SKILL`` (118) is used by the profession/riding
            # acquisition spells instead of SKILL_STEP.  ``EffectBasePoints``
            # carries the rank (riding: 0 = apprentice ... 3 = artisan).
            for effect in template.effects:
                if int(effect.get("effect") or 0) == 118:
                    skill_id = int(effect.get("misc_value") or 0)
                    step = min(6, max(1, int(effect.get("base_points") or 0) + 1))
                    break
        if not skill_id or not step:
            return
        skills = self._player_skills()
        current = skills.get(int(skill_id)) or {}
        if int(current.get("step", 0) or 0) >= step:
            return
        value = int(current.get("value", 0) or 0)
        if value <= 0:
            value = 1
        maximum = trainer.SKILL_MAX_BY_STEP.get(step, 75)
        slot = int(current.get("slot", -1) or -1)
        if slot < 0:
            slot = object_update.next_skill_slot(self.character)
        entry = {
            "slot": slot,
            "value": value,
            "max_value": maximum,
            "step": step,
        }
        skills[int(skill_id)] = entry
        await self._send_player_fields(
            object_update.skill_field_values(self.character, int(skill_id)))
        await run_db_write(
            character_db.set_character_skill,
            self.config.database.world_path, int(self.player_guid),
            int(skill_id), slot, value, maximum, step,
        )
        self.log.info(
            f"Learned skill {skill_id} rank {step} ({value}/{maximum})")

    async def _advance_craft_skill(self, template) -> None:
        """Award a profession skill point after a successful craft."""
        if not self.character or not self.player_guid:
            return
        rows = spells.get_skill_line_ability(
            self.config.database.world_path, int(template.id))
        if not rows:
            return
        char_class = max(1, int(self.character.get("class", 1) or 1))
        char_race = max(1, int(self.character.get("race", 1) or 1))
        for row in rows:
            class_mask = int(row.get("class_mask") or 0)
            race_mask = int(row.get("race_mask") or 0)
            if class_mask and not (class_mask & (1 << (char_class - 1))):
                continue
            if race_mask and not (race_mask & (1 << (char_race - 1))):
                continue
            skill_id = int(row.get("skill_line") or 0)
            skills = self._player_skills()
            current = skills.get(skill_id)
            if current is None:
                continue
            value = int(current.get("value", 0) or 0)
            maximum = int(current.get("max_value", 0) or 0)
            if maximum and value >= maximum:
                return
            low = int(row.get("trivial_low") or 0)
            high = int(row.get("trivial_high") or 0)
            middle = (high + low) // 2
            # ``SkillGainChance``: orange 100%, yellow 75%, green 25%, grey 0.
            if value >= high:
                chance = 0.0
            elif value >= middle:
                chance = 25.0
            elif value >= low:
                chance = 75.0
            else:
                chance = 100.0
            if chance <= 0.0 or random.uniform(0.0, 100.0) >= chance:
                return
            value += 1
            current["value"] = value
            await self._send_player_fields(
                object_update.skill_field_values(self.character, skill_id))
            await run_db_write(
                character_db.set_character_skill,
                self.config.database.world_path, int(self.player_guid),
                skill_id, int(current.get("slot", -1) or -1), value,
                maximum, int(current.get("step", 0) or 0),
            )
            self.log.info(
                f"Craft skill {skill_id} advanced to {value}/{maximum}")
            return

    async def _apply_trainer_proficiency(self, template) -> None:
        """Apply ``SPELL_EFFECT_PROFICIENCY`` (weapon/armour masks)."""
        if template is None:
            return
        item_class, subclass_mask = trainer.proficiency_from_template(template)
        if not item_class or not subclass_mask:
            return
        proficiencies = self.character.setdefault("proficiencies", {})
        combined = int(proficiencies.get(item_class, 0) or 0) | subclass_mask
        proficiencies[item_class] = combined
        await run_db_write(
            character_db.add_character_proficiency,
            self.config.database.world_path, int(self.player_guid),
            item_class, combined,
        )
        await self.send_packet(
            Opcodes.SMSG_SET_PROFICIENCY,
            trainer.build_proficiency(item_class, combined))

    # ------------------------------------------------------------------
    # Opcode handlers
    # ------------------------------------------------------------------
    async def handle_trainer_list(self, data: bytes):
        """``CMSG_TRAINER_LIST``: open the trainer window for a creature."""
        if not self.in_world or not self.character or len(data) < 8:
            return
        guid = int.from_bytes(data[:8], "little")
        spawn = self._trainer_spawn(guid)
        if spawn is None or not self._trainer_in_range(guid):
            return
        if not self._can_use_trainer(spawn):
            return
        await self._send_trainer_list(guid, spawn)

    async def handle_trainer_buy_spell(self, data: bytes):
        """``CMSG_TRAINER_BUY_SPELL``: buy one trainer spell."""
        if not self.in_world or not self.character:
            return
        parsed = trainer.parse_buy_spell(data)
        if parsed is None:
            return
        guid, spell_id = parsed

        async def fail(reason: int) -> None:
            self.log.info(
                f"Trainer buy rejected: guid={guid} spell={spell_id} "
                f"reason={reason}")
            await self.send_packet(
                Opcodes.SMSG_TRAINER_BUY_FAILED,
                trainer.build_buy_failed(guid, spell_id, reason))

        spawn = self._trainer_spawn(guid)
        if spawn is None or not self._trainer_in_range(guid):
            await fail(trainer.TRAINER_FAIL_UNAVAILABLE)
            return
        if not self._can_use_trainer(spawn):
            await fail(trainer.TRAINER_FAIL_UNAVAILABLE)
            return

        path = self.config.database.world_path
        template_id = int(spawn["trainer_template_id"]
                          if "trainer_template_id" in spawn.keys() else 0)
        entries = trainer.get_trainer_spells(
            path, int(spawn["entry"]), template_id)
        trainer_spell = next(
            (entry for entry in entries if entry.spell == int(spell_id)), None)
        if trainer_spell is None:
            await fail(trainer.TRAINER_FAIL_UNAVAILABLE)
            return
        known = self._known_spells()
        state = trainer.spell_state(
            path, trainer_spell, self.character, known, self._player_skills())
        if state != trainer.TRAINER_SPELL_GREEN:
            await fail(trainer.TRAINER_FAIL_UNAVAILABLE)
            return

        cost = int(trainer_spell.cost)
        if cost > int(self.character.get("money", 0) or 0):
            await fail(trainer.TRAINER_FAIL_NOT_ENOUGH_MONEY)
            return

        if cost:
            await self._modify_money(-cost)

        # The client sees the trainer and player visual effects in CMaNGOS's
        # BuySpell handler; they are cosmetic but make the purchase feel right.
        await self.send_packet(
            Opcodes.SMSG_PLAY_SPELL_VISUAL,
            trainer.build_play_spell_visual(guid, 0xB3))
        await self.send_packet(
            Opcodes.SMSG_PLAY_SPELL_IMPACT,
            trainer.build_play_spell_visual(int(self.player_guid or 0), 0x016A))

        new_spells = trainer.learn_spells_for(trainer_spell)
        for new_spell in new_spells:
            previous = trainer.previous_rank(path, new_spell)
            superseded = [previous] if previous in known else []
            await self._learn_spell(new_spell, superseded)

        wrapper = spells.get_spell_template(path, int(trainer_spell.spell))
        await self._apply_trainer_skill(wrapper)
        await self._apply_trainer_proficiency(wrapper)

        await self.send_packet(
            Opcodes.SMSG_TRAINER_BUY_SUCCEEDED,
            trainer.build_buy_succeeded(guid, spell_id))
        self.log.info(
            f"Bought trainer spell {spell_id} from '{spawn['name']}' "
            f"({guid}) for {cost} copper")
