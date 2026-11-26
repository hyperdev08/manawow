"""Player talent learning, reset and client talent data."""
from __future__ import annotations

import struct

from src.common.db_io import run_db_write
from src.protocol.opcodes import Opcodes
from src.world import character_db
from src.world import spells
from src.world import talents as talent_data


class TalentsMixin:
    # ------------------------------------------------------------------
    # State and packets
    # ------------------------------------------------------------------
    def _player_talents(self) -> dict:
        return self.character.setdefault("talents", {})

    async def _load_talents(self) -> None:
        """Restore talent ranks and make sure their spells are known."""
        if not self.character or not self.player_guid:
            return
        path = self.config.database.world_path
        stored = character_db.get_character_talents(
            path, int(self.player_guid))
        self.character["talents"] = {int(k): int(v)
                                     for k, v in stored.items()}
        learned = getattr(self, "known_spell_ids", None)
        if learned is None:
            return
        for talent_id, rank in self.character["talents"].items():
            talent = talent_data.get_talent(path, talent_id)
            if talent is None:
                continue
            spell_id = int(talent[f"spell_rank{int(rank) + 1}"] or 0)
            if spell_id and spell_id not in learned:
                learned.add(spell_id)
                await run_db_write(
                    character_db.learn_character_spell,
                    path, int(self.player_guid), spell_id, ())

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
            template = (spells.get_spell_template(path, spell_id)
                        if spell_id else None)
            if template is not None and template.is_passive():
                await self._apply_spell_effects(
                    None, template, int(self.player_guid))

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
            int(self.character.get("level", 1) or 1))
        return max(0, total - talent_data.used_talent_points(
            self._player_talents()))

    async def handle_learn_talent(self, data: bytes) -> None:
        if not self.in_world or not self.character or len(data) < 8:
            return
        talent_id, requested_rank = struct.unpack_from("<II", data, 0)
        await self._learn_talent(int(talent_id), int(requested_rank))

    async def _learn_talent(self, talent_id: int,
                            requested_rank: int) -> bool:
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
        if requested_rank != current + 1:
            return False
        if requested_rank >= talent_data.MAX_TALENT_RANK:
            return False
        spell_id = int(talent[f"spell_rank{requested_rank + 1}"] or 0)
        if not spell_id:
            return False
        if self._talent_free_points() < 1:
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

        self._player_talents()[talent_id] = requested_rank
        await run_db_write(
            character_db.set_character_talent,
            path, int(self.player_guid), talent_id, requested_rank)

        # Passive talents (and their rank-up effects) are applied immediately.
        template = spells.get_spell_template(path, spell_id)
        if template is not None and template.is_passive():
            await self._apply_spell_effects(
                None, template, int(self.player_guid))
        await self._send_talents_info()
        return True

    async def handle_unlearn_talents(self, data: bytes) -> None:
        if not self.in_world or not self.character:
            return
        await self._reset_talents()

    async def _reset_talents(self) -> None:
        path = self.config.database.world_path
        learned = getattr(self, "known_spell_ids", None)
        for talent_id, _rank in list(self._player_talents().items()):
            talent = talent_data.get_talent(path, int(talent_id))
            if talent is None:
                continue
            for spell_id in set(talent_data.talent_rank_spells(talent)):
                if not spell_id:
                    continue
                if learned is not None and spell_id not in learned:
                    continue
                if hasattr(self, "_remove_player_aura_spell"):
                    await self._remove_player_aura_spell(spell_id)
                if learned is not None:
                    learned.discard(spell_id)
                await run_db_write(
                    character_db.forget_character_spell,
                    path, int(self.player_guid), spell_id)
        self._player_talents().clear()
        await run_db_write(
            character_db.clear_character_talents,
            path, int(self.player_guid))
        await self._send_talents_info()
