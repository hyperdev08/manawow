"""Combat loop, melee swings and attacker state updates."""
from __future__ import annotations

import asyncio
import random
import sqlite3
import struct
import time

from src.protocol.opcodes import Opcodes
from src.world import creature
from src.world import spells

from .constants import (
    COMBAT_TICK_INTERVAL,
    RESPAWN_SCAN_INTERVAL,
)

class CombatMixin:
    # ------------------------------------------------------------------
    # Melee combat
    # ------------------------------------------------------------------
    # A level 1 character without a weapon still swings; these are the numbers
    # used for bare hands (the client animates the attack either way).
    _BARE_HAND = {"min": 1.0, "max": 3.0, "delay": 2.0}

    def start_combat_loop(self) -> None:
        """Start this session's combat/AI task (idempotent)."""
        if getattr(self, "_combat_task", None) is None:
            self._combat_task = asyncio.ensure_future(self._combat_loop())

    def stop_combat_loop(self) -> None:
        task = getattr(self, "_combat_task", None)
        if task is not None:
            task.cancel()
            self._combat_task = None

    async def _combat_loop(self) -> None:
        """Drive swings, casts, creature AI, regeneration and respawns."""
        try:
            while not self.should_close:
                await asyncio.sleep(COMBAT_TICK_INTERVAL)
                if not self.in_world or not self.character:
                    continue
                try:
                    await self._combat_tick(time.monotonic())
                except Exception as exc:  # never let one tick kill the loop
                    self.log.exception(f"Combat tick error: {exc}")
        except asyncio.CancelledError:
            pass

    async def _combat_tick(self, now: float) -> None:
        combat = self._get_combat_state()

        # 1. finish a cast whose casting time elapsed
        pending = combat["pending_cast"]
        if pending and pending["finish_at"] <= now:
            combat["pending_cast"] = None
            await self._apply_pending_cast(pending)

        # 2. the player's auto-attack timer
        if combat["auto_attacking"] and combat["target_guid"] and not combat["dead"]:
            interval = combat["swing_interval"] or 2.0
            if now - combat["last_swing"] >= interval:
                combat["last_swing"] = now
                await self._player_swing(now, self._player_weapon_stats())

        # 3. damage/heal over time
        if combat["dots"]:
            alive = []
            for dot in combat["dots"]:
                if dot["expires"] <= now:
                    continue
                if dot["next_tick"] <= now:
                    dot["next_tick"] = now + dot["interval"]
                    await self._apply_dot_tick(dot)
                alive.append(dot)
            combat["dots"] = alive

        # 4. creatures that are fighting us
        if combat["aggro"] and not combat["dead"]:
            for low_guid in list(combat["aggro"]):
                await self._creature_combat_tick(low_guid, now)

        # 5. idle creatures wander or follow their waypoint path
        await self._creature_idle_tick_all(now)

        # 5b. summoned pets/guardians follow the owner and assist in combat
        await self._summon_tick_all(now)

        # 6. out-of-combat regeneration
        await self._regenerate(now)

        # 7. corpses that have waited long enough come back
        if now - combat["last_respawn_scan"] >= RESPAWN_SCAN_INTERVAL:
            combat["last_respawn_scan"] = now
            await self._respawn_corpses(now)

        # 8. nothing is fighting us any more: clear the combat icon
        if not combat["aggro"] and not combat["auto_attacking"] \
                and not combat["pending_cast"] and combat["in_combat"]:
            combat["in_combat"] = False

        await self._flush_player_fields()

    async def handle_attack_swing(self, data: bytes):
        """``CMSG_ATTACKSWING``: begin auto-attacking the given victim."""
        if not self.in_world or not self.character:
            return
        if len(data) < 8:
            return
        target_guid = struct.unpack_from("<Q", data, 0)[0]
        combat = self._get_combat_state()
        if combat["dead"]:
            return
        low_guid = creature.creature_low_guid(target_guid)
        spawn = self._get_creature_spawn(low_guid)
        if spawn is None:
            return
        if int(low_guid) in getattr(self, "_summoned_creatures", {}):
            # A player cannot attack their own pet; the client should never
            # send this, but a stale selection must not kill the summon.
            return
        if int(spawn["dead_since"] or 0) > 0:
            await self.send_packet(Opcodes.SMSG_ATTACKSWING_DEADTARGET, b"")
            return
        combat["target_guid"] = target_guid
        combat["auto_attacking"] = True
        combat["in_combat"] = True
        # ``last_swing = 0`` makes the very first swing land on the next tick so
        # the attack feels immediate, like the real client's off-hand timer.
        combat["last_swing"] = 0.0
        combat["swing_interval"] = self._player_weapon_stats()["delay"]
        await self.send_packet(
            Opcodes.SMSG_ATTACKSTART,
            struct.pack("<QQ", int(self.player_guid), int(target_guid)),
        )
        self._aggro_creature(low_guid)
        self.log.info(
            f"'{self.character['name']}' begins attacking "
            f"{spawn['name']} ({low_guid})"
        )

    async def handle_attack_stop(self, data: bytes):
        """``CMSG_ATTACKSTOP``: stop the current melee attack."""
        if not self.in_world or not self.character:
            return
        combat = self._get_combat_state()
        target_guid = combat["target_guid"]
        if len(data) >= 8:
            requested = struct.unpack_from("<Q", data, 0)[0]
            if requested and requested != target_guid:
                target_guid = requested
        combat["auto_attacking"] = False
        if target_guid:
            # SMSG_ATTACKSTOP: packed attacker, packed victim, uint32 nowDead.
            payload = (spells.pack_guid(int(self.player_guid))
                       + spells.pack_guid(int(target_guid))
                       + struct.pack("<I", 1 if combat["dead"] else 0))
            await self.send_packet(Opcodes.SMSG_ATTACKSTOP, payload)
        combat["target_guid"] = 0
        await self._update_player_aiface()
        self.log.debug(f"'{self.character['name']}' stops attacking")

    async def _update_player_aiface(self) -> None:
        """Clear the crossed-swords combat icon when nothing is fighting us."""
        combat = self._get_combat_state()
        if combat["aggro"] or combat["auto_attacking"] or combat["pending_cast"]:
            return
        if not combat["in_combat"]:
            return
        combat["in_combat"] = False
        combat["target_guid"] = 0

    async def _player_swing(self, now: float, weapon: dict) -> None:
        """One auto-attack swing against the current target."""
        combat = self._get_combat_state()
        target_guid = combat["target_guid"]
        low_guid = creature.creature_low_guid(target_guid)
        spawn = self._get_creature_spawn(low_guid)
        if spawn is None or int(spawn["dead_since"] or 0) > 0:
            await self.handle_attack_stop(b"")
            return
        if self._distance(self._player_position(),
                          self._creature_position(low_guid)) > self._melee_reach(spawn):
            # Out of reach: the client draws the "out of range" message itself
            # once we simply hold the swing, so do nothing but stay in combat.
            return

        damage = random.uniform(weapon["min"], weapon["max"])
        damage += max(1.0, int(self.character.get("level", 1)) * 0.5)
        missed = random.random() < 0.08
        crit = (not missed) and random.random() < 0.10
        if crit:
            damage *= 2.0
        damage = max(1, int(round(damage)))

        await self._send_attacker_state_update(
            self.player_guid, target_guid, 0 if missed else damage,
            is_miss=missed, is_crit=crit,
        )
        if missed:
            return
        # CMaNGOS rewards the attacker's rage inside ``DealDamage`` *before*
        # the victim's death is processed, so a killing blow still generates
        # rage.  Do it before ``_deal_damage_to_creature`` for the same reason.
        self._gain_rage(damage, weapon_speed=weapon["delay"], crit=crit)
        died = await self._deal_damage_to_creature(target_guid, damage,
                                                   self.player_guid)
        if died:
            await self.handle_attack_stop(b"")

    def _player_weapon_stats(self) -> dict:
        """Return ``{min, max, delay}`` for the equipped main-hand weapon.

        ``character_items.slot`` 15 is ``INVSLOT_MAINHAND``.  ``delay`` is stored
        in milliseconds in ``item_templates`` and becomes the auto-attack swing
        period in seconds.
        """
        con = sqlite3.connect(self.config.database.world_path)
        try:
            row = con.execute(
                "SELECT it.damage_min1, it.damage_max1, it.delay "
                "FROM character_items ci "
                "JOIN item_templates it ON it.entry = ci.item_entry "
                "WHERE ci.character_guid = ? AND ci.slot = 15",
                (int(self.player_guid or 0),),
            ).fetchone()
        finally:
            con.close()
        if not row:
            return dict(self._BARE_HAND)
        low = float(row[0] or 0.0)
        high = float(row[1] or 0.0)
        delay = int(row[2] or 0)
        if high <= 0.0:
            return dict(self._BARE_HAND)
        if low <= 0.0:
            low = max(1.0, high * 0.5)
        return {"min": low, "max": high, "delay": max(800, delay) / 1000.0}

    def _victim_health(self, victim_guid: int) -> int:
        """Health the victim has *before* the hit we are about to report.

        Uses :meth:`_creature_health` rather than the raw ``current_health``
        column: a spawn that has never been damaged stores 0 there, and reading
        it directly would report every first hit as full overkill.
        """
        if self._is_player_guid(victim_guid):
            if not self.character:
                return 0
            return int(self.character.get("health", self._max_health()))
        return self._creature_health(victim_guid)
