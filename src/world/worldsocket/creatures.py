"""Creature spawn state, health, aggro and combat ticks."""
from __future__ import annotations

import asyncio
import json
import math
import random
import time

from src.common.db_io import execute_write, run_db_write, submit_db_write
from src.protocol.opcodes import Opcodes
from src.world import creature
from src.world import login_packets
from src.world import object_update
from src.world import update_fields as F

from .constants import (
    CREATURE_LEASH_RADIUS,
)

class CreaturesMixin:
    # -- world-shared creature state ----------------------------------
    def _creature_combat_manager(self):
        server = getattr(self, "server", None)
        return getattr(server, "creature_combat", None) if server else None

    def _attach_shared_creature(self, state: dict) -> None:
        """Attach the world state to this session's local maps."""
        low_guid = int(state["guid"])
        combat = self._get_combat_state()
        combat["aggro"][low_guid] = state
        combat.setdefault("creature_auras", {})[low_guid] = state.setdefault(
            "auras", [])
        combat.setdefault("creature_diminishing", {})[low_guid] = (
            state.setdefault("diminishing", {}))

    def _forget_shared_creature(self, low_guid: int) -> None:
        combat = self._get_combat_state()
        combat["aggro"].pop(int(low_guid), None)
        combat.setdefault("creature_auras", {}).pop(int(low_guid), None)
        combat.setdefault("creature_diminishing", {}).pop(
            int(low_guid), None)
        # Drop a stale wander destination; the next idle tick should start
        # from the creature's authoritative spawn/home position.
        combat.setdefault("idle", {}).pop(int(low_guid), None)
        combat.setdefault("idle_settle", {})[int(low_guid)] = (
            time.monotonic() + 0.5)

    def _shared_creature_state(self, low_guid: int):
        manager = self._creature_combat_manager()
        if manager is None:
            return None
        return manager.get(int(low_guid))

    def _creature_subscribers(self, low_guid: int) -> list:
        """Sessions that should receive a packet about this creature."""
        low_guid = int(low_guid)
        if low_guid in getattr(self, "_summoned_creatures", {}):
            return [self]
        server = getattr(self, "server", None)
        character = getattr(self, "character", None)
        if server is None or character is None:
            return [self]
        my_map = int(character.get("map", 0))
        result = []
        for session in list(getattr(server, "sessions", ())):
            if not getattr(session, "in_world", False):
                continue
            peer_character = getattr(session, "character", None)
            if not peer_character or session.player_guid is None:
                continue
            if int(peer_character.get("map", 0)) != my_map:
                continue
            if session is not self and low_guid not in getattr(
                    session, "_visible_creatures", {}):
                continue
            if session is self and low_guid not in getattr(
                    self, "_visible_creatures", {}) and not getattr(
                        self, "_summoned_creatures", {}):
                # The acting session may still need the packet even if its
                # visibility cache has not caught up yet.
                result.append(session)
                continue
            result.append(session)
        return result or [self]

    async def _send_creature_packet(self, low_guid: int, opcode: int,
                                    payload: bytes = b"") -> None:
        for session in self._creature_subscribers(low_guid):
            await session.send_packet(opcode, payload)

    def _creature_position(self, low_guid: int) -> tuple[float, float, float]:
        """Where a creature currently is: chase, wander or spawn position.

        A creature that is moving: the client animates it along the
        ``SMSG_MONSTER_MOVE`` splines we send, so the server has to remember the
        destination it handed out or the range checks would keep failing.  An
        engaged creature's position is world-shared, so every session sees the
        same chase/evade position.
        """
        shared = self._shared_creature_state(int(low_guid))
        if shared and shared.get("pos"):
            return tuple(shared["pos"])
        combat = self._get_combat_state()
        tracked = combat["aggro"].get(int(low_guid))
        if tracked and tracked.get("pos"):
            return tuple(tracked["pos"])
        idle = (combat.get("idle") or {}).get(int(low_guid))
        if idle and idle.get("pos"):
            return tuple(idle["pos"])
        spawn = self._get_creature_spawn(int(low_guid))
        if spawn is None:
            return (0.0, 0.0, 0.0)
        return (float(spawn["x"]), float(spawn["y"]), float(spawn["z"]))

    def _creature_object_guid(self, low_guid: int, entry: int = 0) -> int:
        """Full creature guid, packing the template entry the client queries.

        The client reads the creature entry from guid bits 24-47.  When callers
        do not already have the entry we look the spawn up so every packet uses
        exactly the same guid.
        """
        if not entry:
            spawn = self._get_creature_spawn(int(low_guid))
            if spawn is not None:
                entry = int(spawn["entry"])
        return creature.creature_guid(int(low_guid), int(entry))

    async def _send_creature_fields(self, low_guid: int, fields: dict) -> None:
        """Send one ``UPDATETYPE_VALUES`` update to every viewer."""
        opcode, payload = object_update.build_values_update(
            self._creature_object_guid(int(low_guid)), F.UNIT_END, fields,
            force_uncompressed=True,
        )
        await self._send_creature_packet(int(low_guid), opcode, payload)

    def _creature_health(self, guid: int) -> int:
        """Get the current health of a creature from the DB.

        ``dead_since > 0`` marks a corpse even though ``current_health`` is 0
        both for a corpse and for a spawn that has never been damaged.
        """
        low_guid = creature.creature_low_guid(guid)
        spawn = self._get_creature_spawn(low_guid)
        if spawn is None:
            return 0
        if int(spawn["dead_since"] or 0) > 0:
            return 0
        current = int(spawn["current_health"] or 0)
        if current > 0:
            return current
        base = int(spawn["base_health"] or 0) if "base_health" in spawn.keys() else 0
        return base if base > 0 else 100

    def _creature_max_health(self, spawn) -> int:
        base = max(1, int(spawn["base_health"] or 1))
        multiplier = float(spawn["hp_multiplier"] or 1.0)
        return max(1, int(base * multiplier))

    def _set_creature_health(self, low_guid: int, health: int) -> None:
        """Persist a creature's current health (summons stay in memory)."""
        summon = getattr(self, "_summoned_creatures", {}).get(int(low_guid))
        if summon is not None:
            summon["current_health"] = max(0, int(health))
            return
        submit_db_write(
            execute_write, self.config.database.world_path,
            "UPDATE creature_spawns SET current_health = ? WHERE guid = ?",
            (max(0, int(health)), int(low_guid)),
        )

    def _get_creature_spawn(self, low_guid: int):
        """Get a creature spawn row (session summons first, then the DB)."""
        summon = getattr(self, "_summoned_creatures", {}).get(int(low_guid))
        if summon is not None:
            return summon
        return creature.get_creature_spawn(self.config.database.world_path, low_guid)

    async def _deal_damage_to_creature(self, guid: int, damage: int,
                                       attacker_guid: int = 0,
                                       threat_kind: str = "melee") -> bool:
        """Apply damage to a creature. Returns True if it died."""
        low_guid = creature.creature_low_guid(guid)
        if int(low_guid) in getattr(self, "_summoned_creatures", {}):
            return await self._damage_summon(int(low_guid), damage, attacker_guid)
        spawn = self._get_creature_spawn(low_guid)
        if spawn is None:
            return False
        if int(spawn["dead_since"] or 0) > 0:
            return False
        current = self._creature_health(guid)
        if current <= 0:
            return False
        max_health = self._creature_max_health(spawn)
        damage = max(1, int(damage))
        new_health = max(0, current - damage)
        self._set_creature_health(low_guid, new_health)
        combat = self._get_combat_state()
        combat["in_combat"] = True
        self._aggro_creature(low_guid)
        self._add_threat(low_guid, int(attacker_guid or self.player_guid or 0),
                         max(1.0, float(damage)), kind=threat_kind)
        if new_health <= 0:
            await self._on_creature_killed(guid)
            return True
        await self._send_creature_fields(low_guid, {
            F.UNIT_FIELD_HEALTH: new_health,
            F.UNIT_FIELD_MAXHEALTH: max_health,
            F.UNIT_DYNAMIC_FLAGS: 0,
        })
        return False

    def _aggro_creature(self, low_guid: int) -> None:
        """Put a creature into combat with this player (summons excluded)."""
        if int(low_guid) in getattr(self, "_summoned_creatures", {}):
            # A pet is driven by _summon_tick, not by the creature AI/DB state.
            return
        combat = self._get_combat_state()
        now = time.monotonic()
        spawn = self._get_creature_spawn(int(low_guid))
        if spawn is None:
            return
        entry = combat["aggro"].get(int(low_guid))
        if entry is None:
            # Start the fight from wherever the creature wandered to; otherwise
            # the first chase spline would teleport it back to its spawn point.
            idle = combat.setdefault("idle", {}).pop(int(low_guid), None)
            start_pos = idle.get("pos") if idle else None
            manager = self._creature_combat_manager()
            if manager is not None:
                # World-shared aggro: every session gets the same dictionary,
                # so threat, target and predicted position cannot diverge.
                state = manager.engage(
                    int(low_guid), spawn, int(self.player_guid or 0),
                    start_pos=start_pos)
                combat["aggro"][int(low_guid)] = state
            else:
                combat["aggro"][int(low_guid)] = {
                    "guid": int(low_guid),
                    "entry": int(spawn["entry"] or 0),
                    "map": (int(spawn["map"] or 0)
                            if "map" in spawn.keys()
                            else int(self.character.get("map", 0) or 0)),
                    "last_swing": now - 1.0,
                    "chase_until": 0.0,
                    "pos": start_pos,
                    "facing": None,
                    "face_until": 0.0,
                    # Lazily built on the first AI tick: spell list, mana pool,
                    # cooldowns and the spell currently being cast.
                    "ai": None,
                    # 3.3.5-style threat table, keyed by player guid.
                    "threat": 0.0,
                    "threat_table": {int(self.player_guid or 0): 0.0},
                    "target_guid": int(self.player_guid or 0),
                    "taunt_target": 0,
                    "taunt_until": 0.0,
                    "fleeing": False,
                    "flee_until": 0.0,
                    "last_mana_regen": now,
                    "last_power_update": now,
                }
            self._call_for_help(int(low_guid))
        submit_db_write(
            execute_write, self.config.database.world_path,
            "UPDATE creature_spawns SET combat_state = 1, target_guid = ? "
            "WHERE guid = ? AND dead_since = 0",
            (int(self.player_guid or 0), int(low_guid)),
        )

    def _threat_source_guid(self, source_guid: int) -> int:
        """Attribute pet damage to the pet's owner, not the pet guid."""
        source_guid = int(source_guid or 0)
        if source_guid and self._is_player_guid(source_guid):
            return int(self.player_guid or source_guid)
        if source_guid and hasattr(self, "_is_peer_player_guid") \
                and self._is_peer_player_guid(source_guid):
            return source_guid
        low = creature.creature_low_guid(source_guid) if source_guid else 0
        summon = getattr(self, "_summoned_creatures", {}).get(low)
        if summon is not None:
            return int(summon.get("summoned_by")
                       or self.player_guid or 0)
        return int(self.player_guid or 0)

    def _add_threat(self, low_guid: int, source_guid: int,
                    amount: float, kind: str = "melee") -> None:
        """Add threat to a creature's table (``ThreatManager::AddThreat``)."""
        source_guid = self._threat_source_guid(source_guid)
        redirect = self._get_combat_state().get("threat_redirect")
        if redirect and int(redirect.get("until") or 0.0) > time.monotonic() \
                and source_guid == int(self.player_guid or 0):
            source_guid = int(redirect.get("guid") or source_guid)
        manager = self._creature_combat_manager()
        if manager is not None and manager.is_engaged(int(low_guid)):
            manager.add_threat(int(low_guid), source_guid, amount, kind=kind)
            return
        state = self._get_combat_state().get("aggro", {}).get(int(low_guid))
        if state is None:
            return
        table = state.setdefault("threat_table", {})
        key = int(source_guid or self.player_guid or 0)
        table[key] = max(0.0, float(table.get(key, 0.0)) + float(amount))
        state["threat"] = max(float(state.get("threat", 0.0)),
                              float(table[key]))
        state["target_guid"] = int(self.player_guid or 0)

    def _call_for_help(self, low_guid: int) -> None:
        """Nearby same-faction creatures join a fight (social aggro)."""
        combat = self._get_combat_state()
        spawn = self._get_creature_spawn(int(low_guid))
        if spawn is None:
            return
        home = (float(spawn["x"]), float(spawn["y"]), float(spawn["z"]))
        faction = int(spawn["faction"] or 0) if "faction" in spawn.keys() else 0
        helped = 0
        for other_low, other in list(self._visible_creatures.items()
                                     if hasattr(self, "_visible_creatures")
                                     else ()):
            other_low = int(other_low)
            if other_low == int(low_guid) or other_low in combat["aggro"]:
                continue
            other_spawn = self._get_creature_spawn(other_low)
            if other_spawn is None or int(other_spawn["dead_since"] or 0) > 0:
                continue
            if other_low in getattr(self, "_summoned_creatures", {}):
                continue
            other_faction = int(other_spawn["faction"] or 0) \
                if "faction" in other_spawn.keys() else 0
            if faction and other_faction and faction != other_faction:
                continue
            pos = (float(other_spawn["x"]), float(other_spawn["y"]),
                   float(other_spawn["z"]))
            if self._distance(home, pos) > 12.0:
                continue
            self._aggro_creature(other_low)
            helped += 1
            if helped >= 3:
                break
        if helped:
            self.log.debug(
                f"Creature {spawn['entry']} called {helped} ally/allies for help")

    @staticmethod
    def _xp_for_kill(player_level: int, creature_level: int) -> int:
        level = max(1, int(creature_level))
        player_level = max(1, int(player_level))
        base_xp = level * 5 + 45
        if player_level >= level + 5:
            return 0
        if player_level <= level:
            return base_xp
        return int(base_xp * max(
            0.1, 1.0 - (player_level - level) * 0.1))

    @staticmethod
    def _player_position_for(session) -> tuple[float, float, float]:
        return (float(session.character.get("x", 0.0) or 0.0),
                float(session.character.get("y", 0.0) or 0.0),
                float(session.character.get("z", 0.0) or 0.0))

    async def _on_creature_killed(self, guid: int) -> None:
        """Handle creature death: stop combat, award XP and make a corpse."""
        low_guid = creature.creature_low_guid(guid)
        spawn = self._get_creature_spawn(low_guid)
        if spawn is None:
            return
        manager = self._creature_combat_manager()
        shared = manager.get(low_guid) if manager is not None else None
        tagger_guid = int(
            (shared or {}).get("tagger_guid")
            or (shared or {}).get("target_guid")
            or self.player_guid or 0)
        tagger = (manager.session_for(tagger_guid)
                  if manager is not None else None)
        if tagger is None or not getattr(tagger, "in_world", False):
            tagger = self
            tagger_guid = int(self.player_guid or 0)
        entry = int(spawn["entry"])
        level = max(1, int(spawn["min_level"])) if "min_level" in spawn.keys() else 1
        corpse_pos = (float(spawn["x"]), float(spawn["y"]),
                      float(spawn["z"]))
        award_sessions = [tagger] if tagger is not None else []
        group_manager = getattr(getattr(self, "server", None), "groups", None)
        group = (group_manager.group_for(tagger_guid)
                 if group_manager is not None else None)
        if group is not None:
            award_sessions = []
            map_id = (int(spawn["map"] or 0)
                      if "map" in spawn.keys()
                      else int(self.character.get("map", 0) or 0))
            for session in group_manager.member_sessions(group, map_id):
                if not session.character:
                    continue
                if session._get_combat_state().get("dead"):
                    continue
                pos = self._player_position_for(session)
                if self._distance(corpse_pos, pos) > 100.0:
                    continue
                award_sessions.append(session)
            if tagger is not None and tagger not in award_sessions:
                award_sessions.append(tagger)
        group_bonus = min(1.5, 1.0 + 0.1 * max(0, len(award_sessions) - 1))
        xp_awards = []
        for session in award_sessions:
            player_level = int(session.character.get("level", 1))
            xp = self._xp_for_kill(player_level, level)
            if xp > 0:
                xp_awards.append((session, int(xp * group_bonus)))
        xp = sum(value for _session, value in xp_awards)
        # Generate loot ONCE when the creature dies.  CMaNGOS stores the loot
        # template id separately from the creature entry; 190 creatures (Naxx
        # and a few event NPCs) share another creature's loot table.
        loot_id = int(spawn["loot_id"] or 0) if "loot_id" in spawn.keys() else 0
        loot_items = self._generate_loot(entry, loot_id or entry)
        loot_money = self._loot_money(spawn)
        loot_json = json.dumps({"items": loot_items, "money": loot_money})
        await run_db_write(
            execute_write, self.config.database.world_path,
            "UPDATE creature_spawns SET current_health = 0, dead_since = ?, "
            "combat_state = 3, target_guid = 0, loot_json = ?, "
            "loot_owner = ? WHERE guid = ?",
            (int(time.time()), loot_json, int(tagger_guid), low_guid),
        )
        combat = self._get_combat_state()
        # ``Unit::ClearComboPointHolders``: a target's death drops every
        # player's combo points on it.  Only this session can hold points, so
        # clearing them here is the whole job.
        if int(combat.get("combo_target") or 0) == int(guid):
            await self._clear_combo_points()
        combat["aggro"].pop(low_guid, None)
        if hasattr(self, "_clear_creature_auras"):
            self._clear_creature_auras(low_guid)
        if manager is not None:
            manager.forget(low_guid)
            manager.forget_idle(low_guid)
        if hasattr(self, "_sync_creature_flags"):
            await self._sync_creature_flags(int(low_guid))
        combat.setdefault("idle", {}).pop(low_guid, None)
        combat.setdefault("idle_settle", {}).pop(low_guid, None)
        self._creature_spawn_cache.pop(low_guid, None)
        await self._send_creature_fields(low_guid, {
            F.UNIT_FIELD_HEALTH: 0,
            F.UNIT_DYNAMIC_FLAGS: 1,      # UNIT_DYNFLAG_LOOTABLE
        })
        for session, award in xp_awards:
            await session._award_xp(award, guid)
        # ``Player::RewardPlayerAndGroupAtKill``: every rewarded group member
        # in range gets kill credit, even when the kill is grey for them.
        for session in award_sessions:
            if hasattr(session, "_quest_on_creature_killed"):
                await session._quest_on_creature_killed(int(guid), entry)
        self.log.info(
            f"Creature {entry} ({low_guid}) killed by {tagger_guid}, "
            f"awarded {xp} XP to {len(xp_awards)} player(s)")

    async def _creature_combat_tick(self, low_guid: int, now: float) -> None:
        """One AI tick for a creature that is fighting this player."""
        if int(low_guid) in getattr(self, "_summoned_creatures", {}):
            return
        combat = self._get_combat_state()
        state = combat["aggro"].get(int(low_guid))
        if state is None:
            return
        # The current threat target's session is the single AI driver.  Other
        # sessions keep the shared aggro entry for the combat icon/visibility,
        # but must not run a second copy of the creature's AI.
        manager = self._creature_combat_manager()
        if manager is not None and manager.is_engaged(int(low_guid)):
            target_guid = int(state.get("target_guid") or 0)
            if not target_guid or target_guid != int(self.player_guid or 0):
                return
        spawn = self._get_creature_spawn(int(low_guid))
        if spawn is None or int(spawn["dead_since"] or 0) > 0:
            if manager is not None:
                manager.forget(int(low_guid))
            else:
                combat["aggro"].pop(int(low_guid), None)
            return

        # Low-health humanoids flee instead of fighting to the death
        # (``CreatureAI::DamageTaken`` -> ``SetCombatMovement``).  Fleeing
        # creatures run away from the player and stop attacking/casting.
        creature_type = int(spawn["type"] or 0) if "type" in spawn.keys() else 0
        health = self._creature_health(int(low_guid))
        maximum = self._creature_max_health(spawn)
        if (creature_type == 7 and maximum > 0
                and health > maximum * 0.30 and state.get("fled")):
            state["fled"] = False
        if (creature_type == 7 and maximum > 0
                and health < maximum * 0.15
                and not state.get("fleeing")
                and not state.get("fled")):
            state["fleeing"] = True
            state["flee_until"] = now + 6.0
            if state.get("ai"):
                state["ai"]["cast"] = None
            self.log.debug(f"Creature {spawn['entry']} is fleeing")
        if state.get("fleeing"):
            if now >= float(state.get("flee_until") or 0.0):
                state["fleeing"] = False
                state["fled"] = True
            else:
                await self._creature_flee_move(int(low_guid), spawn, state,
                                               now)
                return

        # Stunned creatures do nothing at all; rooted ones can still swing and
        # cast while in range but cannot chase.
        if hasattr(self, "_creature_aura_mods"):
            mods = self._creature_aura_mods(int(low_guid))
            if mods["stunned"]:
                ai = (state.get("ai") if state else None)
                if ai is not None and ai.get("cast") is not None:
                    ai["cast"] = None
                return
            if mods["feared"] or mods["confused"]:
                ai = (state.get("ai") if state else None)
                if ai is not None:
                    ai["cast"] = None
                if mods["rooted"]:
                    return
                await self._creature_flee_move(int(low_guid), spawn, state, now)
                return
            if mods["charmed"]:
                ai = (state.get("ai") if state else None)
                if ai is not None:
                    ai["cast"] = None
                return
            rooted = mods["rooted"]
            pacified = mods["pacified"]
            melee_interval = self._creature_melee_interval(int(low_guid))
            speed_mult = self._creature_speed_mult(int(low_guid))
        else:
            rooted = pacified = False
            melee_interval = 2.0
            speed_mult = 1.0

        player = self._player_position()
        here = self._creature_position(int(low_guid))
        distance = self._distance(player, here)
        reach = self._melee_reach(spawn)

        # Leash: only the victim's distance from the spawn counts
        # (``Creature::CanCreatureAttack``).  The creature's own position must
        # not: a low-health humanoid that just fled ~40 yards would otherwise
        # reset to full health as soon as its flee ended.
        home = (float(spawn["x"]), float(spawn["y"]), float(spawn["z"]))
        if self._distance(home, player) > CREATURE_LEASH_RADIUS:
            await self._reset_creature(int(low_guid))
            return

        in_melee = distance <= reach
        ai = await self._creature_ai_state(int(low_guid), spawn, now)

        # Creatures regenerate mana in combat from their class base pool.  The
        # update packet is throttled to once every two seconds.
        if ai is not None and int(ai.get("max_mana") or 0) > 0:
            last_regen = float(ai.get("last_mana_regen") or now)
            if now - last_regen >= 1.0:
                ai["last_mana_regen"] = now
                gain = max(1, int(ai["max_mana"]) // 50)
                ai["mana"] = min(int(ai["max_mana"]),
                                 int(ai["mana"]) + gain)
                if now - float(ai.get("last_power_update") or 0.0) >= 2.0:
                    ai["last_power_update"] = now
                    self._set_creature_power(int(low_guid), ai["mana"])
                    await self._send_creature_power(int(low_guid), ai)

        # A creature that is casting stands still, faces its victim and neither
        # swings nor chases until the cast has finished (Spell::update runs
        # before the AI tick in the reference core).
        if ai is not None and ai["cast"] is not None:
            if now >= ai["cast"]["finish_at"]:
                await self._finish_creature_cast(int(low_guid), spawn, ai)
            else:
                # A casting creature turns towards its victim but does not move.
                await self._face_creature_at_player(int(low_guid), state,
                                                    spawn, here, player, now)
                return

        # ``UnitAI::UpdateAI`` evaluates the spell list before melee.
        if ai is not None:
            await self._creature_spell_list_tick(
                int(low_guid), spawn, ai, now, in_melee)
            if ai["cast"] is not None:
                return

        if distance <= reach:
            # In melee range the creature stops moving, so its orientation only
            # changes when we explicitly send a face spline.  Without this it
            # keeps staring at whatever direction it spawned with while the
            # player circles around it.
            await self._face_creature_at_player(int(low_guid), state, spawn,
                                                here, player, now)
            if hasattr(self, "_has_line_of_sight") and not await self._has_line_of_sight(
                    self._unit_eye_position(
                        self._creature_object_guid(
                            int(low_guid), int(spawn["entry"]))),
                    self._unit_eye_position(self.player_guid)):
                return
            # In melee range: swing on the creature's own timer.
            if not pacified and now - state["last_swing"] >= melee_interval:
                state["last_swing"] = now
                if hasattr(self, "_player_aura_mods"):
                    immune = self._player_aura_mods()
                    if (immune.get("school_immune", 0) & 1
                            or immune.get("damage_immune", 0) & 1):
                        await self._send_attacker_state_update(
                            self._creature_object_guid(
                                int(low_guid), int(spawn["entry"])),
                            self.player_guid, 0, is_miss=True)
                        return
                creature_level = max(1, int(spawn["min_level"] or 1))
                outcome = self._roll_melee(
                    self._creature_melee_table(creature_level))
                if outcome == "miss":
                    await self._send_attacker_state_update(
                        self._creature_object_guid(
                            int(low_guid), int(spawn["entry"])),
                        self.player_guid, 0, is_miss=True)
                    return
                if outcome in ("dodge", "parry"):
                    await self._send_attacker_state_update(
                        self._creature_object_guid(
                            int(low_guid), int(spawn["entry"])),
                        self.player_guid, 0,
                        victim_state=2 if outcome == "dodge" else 3)
                    return
                raw_damage = max(1, int(round(self._creature_damage(spawn))))
                # ``Unit::CalcArmorReducedDamage``: normal-school melee is
                # mitigated by the player's armor (item armor + agility*2)
                # before it reaches the health pool.
                damage = self._armor_reduced_damage(
                    raw_damage, int(self.character.get("armor", 0) or 0),
                    creature_level)
                hit_info = 0
                victim_state = 1
                if outcome == "block":
                    blocked = max(
                        int(self.character.get("block_value", 0) or 0),
                        int(damage * 0.3))
                    damage = max(1, damage - blocked)
                    hit_info |= 0x00002000      # HITINFO_BLOCK
                    victim_state = 5           # VICTIMSTATE_BLOCKS
                elif outcome == "crushing":
                    damage = int(damage * 1.5)
                    hit_info |= 0x00020000      # HITINFO_CRUSHING
                elif outcome == "crit":
                    damage *= 2
                if hasattr(self, "_apply_damage_modifiers"):
                    damage = self._apply_damage_modifiers(
                        "player", self.player_guid, 1, damage)
                await self._send_attacker_state_update(
                    self._creature_object_guid(int(low_guid), int(spawn["entry"])),
                    self.player_guid, damage,
                    is_crit=(outcome == "crit"),
                    victim_state=victim_state, hit_info=hit_info,
                )
                # Creature auto-attacks are normal-school melee, the only
                # damage source that fills a warrior's rage bar.
                await self._damage_player(damage, str(spawn["name"]),
                                          physical=True)
                if hasattr(self, "_break_cc_on_damage"):
                    await self._break_cc_on_damage(
                        "player", self.player_guid, 0)
                # Thorns/Damage Shield retaliates against the melee attacker.
                if hasattr(self, "_player_aura_mods"):
                    thorns = int(self._player_aura_mods().get("thorns", 0) or 0)
                    if thorns > 0:
                        await self._deal_damage_to_creature(
                            self._creature_object_guid(
                                int(low_guid), int(spawn["entry"])),
                            thorns, self.player_guid,
                            threat_kind="spell")
                        await self._send_spell_damage_log(
                            self._creature_object_guid(
                                int(low_guid), int(spawn["entry"])),
                            self.player_guid, 0, thorns, 1)
            return

        # Out of reach: walk towards the player.  One spline per second keeps
        # the packet rate sane while still looking smooth to the client.
        if rooted:
            return
        if now < state["chase_until"]:
            return
        speed = 7.0 * max(0.1, speed_mult)
        duration_ms = max(250, int((distance - reach * 0.5) / speed * 1000))
        duration_ms = min(duration_ms, 1500)
        travel = min(distance - 1.0, speed * duration_ms / 1000.0)
        if travel <= 0.1:
            return

        # Open-field chases stay straight; a wall, hill or WMO sends the
        # creature through the extracted navigation mesh instead so it walks
        # around the obstacle rather than into it.
        path = None
        if hasattr(self, "_find_world_path"):
            eye_a = (here[0], here[1], here[2] + 0.9)
            eye_b = (player[0], player[1], player[2] + 0.9)
            if not await self._has_line_of_sight(eye_a, eye_b):
                path = await self._find_world_path(here, player)
                if path is None and hasattr(self, "_has_navmesh_at") \
                        and self._has_navmesh_at(here) \
                        and self._has_navmesh_at(player):
                    # Both ends are on real navmesh but no corridor exists.
                    # Do not fake a straight line through a wall; retry a
                    # little later instead of running A* every AI tick.
                    state["chase_until"] = now + 0.5
                    return
        points = None
        if path and len(path) > 1:
            points = self._path_prefix(here, path, travel,
                                       stop_distance=reach * 0.5)
            if not points:
                return
            dest = points[-1]
            moved = sum(self._distance(a, b) for a, b in
                        zip([here] + points[:-1], points))
            if moved <= 0.1:
                return
            duration_ms = max(250, min(1500, int(moved / speed * 1000.0)))
        else:
            scale = travel / distance
            dest = (here[0] + (player[0] - here[0]) * scale,
                    here[1] + (player[1] - here[1]) * scale,
                    here[2] + (player[2] - here[2]) * scale)
        state["pos"] = dest
        state["chase_until"] = now + duration_ms / 1000.0
        facing = math.atan2(dest[1] - here[1], dest[0] - here[0])
        state["facing"] = facing
        await self._send_creature_packet(
            int(low_guid), Opcodes.SMSG_MONSTER_MOVE,
            object_update.build_monster_move_path(
                self._creature_object_guid(int(low_guid), int(spawn["entry"])),
                here[0], here[1], here[2], facing,
                points if points else [dest],
                duration_ms=duration_ms,
                move_id=self._next_move_id(),
            ),
        )

    async def _creature_flee_move(self, low_guid: int, spawn, state: dict,
                                  now: float) -> None:
        """Run a fleeing creature directly away from the player."""
        if now < float(state.get("chase_until") or 0.0):
            return
        here = self._creature_position(int(low_guid))
        player = self._player_position()
        dx = here[0] - player[0]
        dy = here[1] - player[1]
        length = max(0.1, (dx * dx + dy * dy) ** 0.5)
        distance = 8.0
        dest = (here[0] + dx / length * distance,
                here[1] + dy / length * distance, here[2])
        # Snap the run-away point onto walkable ground; otherwise a flee can
        # pick the far side of a wall and the client receives an invalid spline.
        snapped = (await self._navmesh_point((dest[0], dest[1], here[2]),
                                             max_distance=6.0)
                   if hasattr(self, "_navmesh_point") else None)
        if snapped is not None:
            dest = snapped
        path = None
        if hasattr(self, "_find_world_path"):
            if not await self._has_line_of_sight(
                    (here[0], here[1], here[2] + 0.9),
                    (dest[0], dest[1], dest[2] + 0.9)):
                path = await self._find_world_path(here, dest)
                if path is None and hasattr(self, "_has_navmesh_at") \
                        and self._has_navmesh_at(here) \
                        and self._has_navmesh_at(dest):
                    state["chase_until"] = now + 0.5
                    return
        points = None
        if path and len(path) > 1:
            points = self._path_prefix(here, path, 12.0)
            if points:
                dest = points[-1]
        duration_ms = 1000
        state["pos"] = dest
        state["chase_until"] = now + duration_ms / 1000.0
        facing = math.atan2(dest[1] - here[1], dest[0] - here[0])
        state["facing"] = facing
        await self._send_creature_packet(
            int(low_guid), Opcodes.SMSG_MONSTER_MOVE,
            object_update.build_monster_move_path(
                self._creature_object_guid(int(low_guid), int(spawn["entry"])),
                here[0], here[1], here[2], facing,
                points if points else [dest],
                duration_ms=duration_ms,
                move_id=self._next_move_id(),
            ),
        )

    async def _reset_creature(self, low_guid: int) -> None:
        """Leave combat, walk the creature home and heal it back up."""
        combat = self._get_combat_state()
        here = self._creature_position(int(low_guid))
        state = combat["aggro"].pop(int(low_guid), None)
        manager = self._creature_combat_manager()
        if hasattr(self, "_clear_creature_auras"):
            self._clear_creature_auras(int(low_guid))
        if manager is not None:
            manager.forget(int(low_guid))
            manager.forget_idle(int(low_guid))
        if hasattr(self, "_sync_creature_flags"):
            await self._sync_creature_flags(int(low_guid))
        spawn = self._get_creature_spawn(int(low_guid))
        if spawn is None or int(spawn["dead_since"] or 0) > 0:
            return
        max_health = self._creature_max_health(spawn)
        self._set_creature_health(int(low_guid), max_health)
        await self._send_creature_fields(int(low_guid), {
            F.UNIT_FIELD_HEALTH: max_health,
            F.UNIT_FIELD_MAXHEALTH: max_health,
        })
        # Evading also resets power to full (``Creature::RegenerateAll``), so a
        # caster that ran itself dry does not start the next fight empty.
        ai = state.get("ai") if state else None
        if ai and int(ai.get("max_mana") or 0) > 0:
            ai["mana"] = int(ai["max_mana"])
            self._set_creature_power(int(low_guid), ai["mana"])
            await self._send_creature_power(int(low_guid), ai)
        home = (float(spawn["x"]), float(spawn["y"]), float(spawn["z"]))
        await run_db_write(
            execute_write, self.config.database.world_path,
            "UPDATE creature_spawns SET combat_state = 0, target_guid = 0 "
            "WHERE guid = ?",
            (int(low_guid),),
        )
        distance = self._distance(home, here)
        settle_until = time.monotonic()
        can_move = True
        if distance > 1.0:
            path = None
            if hasattr(self, "_find_world_path"):
                if not await self._has_line_of_sight(
                        (here[0], here[1], here[2] + 0.9),
                        (home[0], home[1], home[2] + 0.9)):
                    path = await self._find_world_path(here, home)
                    if path is None and hasattr(self, "_has_navmesh_at") \
                            and self._has_navmesh_at(here) \
                            and self._has_navmesh_at(home):
                        can_move = False
            points = None
            if path and len(path) > 1:
                points = self._path_prefix(here, path, 7.0 * 6.0)
                if points:
                    home = points[-1]
                    distance = sum(self._distance(a, b) for a, b in
                                   zip([here] + points[:-1], points))
            duration_ms = max(250, int(distance / 7.0 * 1000))
            duration_ms = min(duration_ms, 6000)
            settle_until = time.monotonic() + duration_ms / 1000.0
            if can_move:
                await self._send_creature_packet(
                    int(low_guid), Opcodes.SMSG_MONSTER_MOVE,
                    object_update.build_monster_move_path(
                        self._creature_object_guid(int(low_guid), int(spawn["entry"])),
                        here[0], here[1], here[2],
                        math.atan2(home[1] - here[1], home[0] - here[0]),
                        points if points else [home],
                        duration_ms=duration_ms,
                        move_id=self._next_move_id(),
                    ),
                )
        # Restart the idle generator at home, but only after the walk-home
        # spline has finished so it cannot immediately override it.
        combat.setdefault("idle", {}).pop(int(low_guid), None)
        combat.setdefault("idle_settle", {})[int(low_guid)] = settle_until + 0.5
        if state is not None:
            self.log.debug(f"Creature {spawn['entry']} left combat")

    async def _respawn_corpses(self, now: float) -> None:
        """Re-create dead spawns whose respawn timer expired.

        The scan runs every two seconds.  One bulk query (off the event loop)
        replaces the old query-per-visible-creature loop, which was a periodic
        stall while the player moved through a crowded zone.
        """
        if not self._visible_creatures:
            return
        statuses = await asyncio.to_thread(
            creature.get_creature_respawn_statuses,
            self.config.database.world_path,
            list(self._visible_creatures),
        )
        if not statuses:
            return
        now_seconds = int(time.time())
        rows = [
            int(low_guid) for low_guid, (dead_since, respawn_time) in
            statuses.items()
            if dead_since + max(5, respawn_time) <= now_seconds
        ]
        if not rows:
            return
        for low_guid in rows:
            await run_db_write(
                execute_write, self.config.database.world_path,
                "UPDATE creature_spawns SET dead_since = 0, combat_state = 0, "
                "target_guid = 0, current_health = 0, current_power = 0, "
                "loot_json = '', loot_owner = 0 "
                "WHERE guid = ?",
                (int(low_guid),),
            )
            manager = self._creature_combat_manager()
            if manager is not None:
                manager.forget(int(low_guid))
                manager.forget_idle(int(low_guid))
            groups = getattr(getattr(self, "server", None), "groups", None)
            if groups is not None:
                groups.forget_corpse(int(low_guid))
            combat = self._get_combat_state()
            combat["aggro"].pop(int(low_guid), None)
            combat.setdefault("idle", {}).pop(int(low_guid), None)
            if hasattr(self, "_clear_creature_auras"):
                self._clear_creature_auras(int(low_guid))
            combat.setdefault("idle_settle", {}).pop(int(low_guid), None)
            self._creature_spawn_cache.pop(int(low_guid), None)
            if combat["loot_target"] and creature.creature_low_guid(
                    combat["loot_target"]) == int(low_guid):
                await self._close_loot()
            if self.player_guid and low_guid in self._visible_creatures:
                object_guid = self._visible_creatures.pop(low_guid)
                await self.send_packet(
                    Opcodes.SMSG_DESTROY_OBJECT,
                    login_packets.build_destroy_object(object_guid, death=True),
                )
            self.log.debug(f"Creature spawn {low_guid} respawned")
        # One rescan for the whole batch.  The respawned creatures are sent in
        # the same create update instead of starting a visibility scan per
        # corpse.
        await self.refresh_creature_visibility(force=True)

    def _melee_reach(self, spawn) -> float:
        """Distance at which this session can hit ``spawn`` (yard, centre to centre)."""
        reach = 3.0
        if "combat_reach" in spawn.keys():
            try:
                reach = float(spawn["combat_reach"] or 0.0) + 3.0
            except (TypeError, ValueError):
                reach = 3.0
        return min(max(reach, 3.0), 15.0)

    def _creature_damage(self, spawn) -> float:
        """A creature's melee damage roll, scaled from its level."""
        level = max(1, int(spawn["min_level"] or 1))
        low = float(spawn["min_damage"] or 0.0)
        high = float(spawn["max_damage"] or 0.0)
        if high <= 0.0:
            low = max(1.0, level * 0.9)
            high = max(low + 1.0, level * 1.6)
        else:
            low = max(1.0, low)
        attack_power = int(spawn["attack_power"] or 0)
        if hasattr(self, "_creature_aura_mods"):
            aura_low = spawn["guid"] if "guid" in spawn.keys() else 0
            mods = self._creature_aura_mods(int(aura_low))
            attack_power += int(mods.get("attack_power", 0) or 0)
            attack_power = int(attack_power * (
                1.0 + float(mods.get("attack_power_pct", 0.0) or 0.0) / 100.0))
        if attack_power > 0:
            delay = max(800, int(spawn["melee_attack_speed"] or 2000)) / 1000.0
            low += attack_power / 14.0 * delay
            high += attack_power / 14.0 * delay
        return random.uniform(low, high)
