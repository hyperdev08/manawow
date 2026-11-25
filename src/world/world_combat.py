"""World-scoped creature combat state.

Before this module, every ``WorldSocket`` kept its own ``combat["aggro"]``
dictionary.  That works for one player, but two players fighting the same mob
each had a private target, threat table and predicted position: the creature
could chase two different people at once, and one client's hit could make the
other client's health bar jump.

``WorldCombatManager`` owns one state dictionary per engaged creature spawn.
Sessions attach the *same* dictionary object to their local ``aggro`` map, so
the existing AI/movement code keeps working while seeing shared values.

The session of the creature's current threat target is the AI driver.  Only
that session runs ``_creature_combat_tick`` for the creature; other sessions
still know it is in combat (so idle movement is suppressed and the combat icon
stays correct) but must not tick it.  If the target changes because of threat,
the new target's session naturally becomes the driver on its next tick.

This is deliberately an incremental layer: aggro/threat/target/position are
shared, while spell AI, auras and loot remain in their existing modules for
later steps.
"""
from __future__ import annotations

import time
from typing import Optional

# 3.3.5 switches a mob's melee target when the new source reaches 110% of the
# current target's threat (ranged threat uses 130%; that distinction needs the
# combat source type and is a later refinement).
TARGET_SWITCH_FACTOR = 1.10
TARGET_SWITCH_FACTOR_RANGED = 1.30


class WorldCombatManager:
    def __init__(self, server=None):
        self.server = server
        self._states: dict[int, dict] = {}
        self._idle: dict[int, dict] = {}
        self._idle_driver: dict[int, tuple[int, float]] = {}
        # Creature health is authoritative in memory.  Reading the row back
        # after every hit loses damage when two hits land in one combat tick
        # (the queued DB write has not run yet).
        self._health: dict[int, int] = {}

    # -- creature health ------------------------------------------------
    def get_health(self, low_guid: int, spawn) -> Optional[int]:
        low_guid = int(low_guid)
        if low_guid in self._health:
            return self._health[low_guid]
        if spawn is None:
            return None
        current = int(spawn["current_health"] or 0)
        if current > 0:
            return current
        base = int(spawn["base_health"] or 0) if "base_health" in spawn.keys() else 0
        return base if base > 0 else 100

    def set_health(self, low_guid: int, health: int) -> None:
        self._health[int(low_guid)] = max(0, int(health))

    def forget_health(self, low_guid: int) -> None:
        self._health.pop(int(low_guid), None)

    # -- session helpers ------------------------------------------------
    def _sessions(self):
        server = self.server
        if server is None:
            return ()
        return tuple(getattr(server, "sessions", ()))

    def session_for(self, player_guid: int):
        player_guid = int(player_guid or 0)
        if not player_guid:
            return None
        for session in self._sessions():
            if getattr(session, "player_guid", None) == player_guid:
                return session
        return None

    def player_alive(self, player_guid: int, map_id: int) -> bool:
        session = self.session_for(player_guid)
        if session is None or not getattr(session, "in_world", False):
            return False
        character = getattr(session, "character", None)
        if not character:
            return False
        if int(character.get("map", 0)) != int(map_id):
            return False
        combat = session._get_combat_state()
        return not bool(combat.get("dead"))

    def _attach(self, player_guid: int, state: dict) -> None:
        session = self.session_for(player_guid)
        if session is None or not getattr(session, "in_world", False):
            return
        attach = getattr(session, "_attach_shared_creature", None)
        if attach is not None:
            attach(state)

    # -- state lifecycle ------------------------------------------------
    def get(self, low_guid: int) -> Optional[dict]:
        return self._states.get(int(low_guid))

    def is_engaged(self, low_guid: int) -> bool:
        return int(low_guid) in self._states

    # -- shared idle movement ------------------------------------------
    def idle_state(self, low_guid: int, factory) -> dict:
        low_guid = int(low_guid)
        state = self._idle.get(low_guid)
        if state is None:
            state = factory()
            self._idle[low_guid] = state
        return state

    def claim_idle(self, low_guid: int, player_guid: int,
                   now: float, lease: float = 1.0) -> bool:
        """Let one visible session drive a creature's idle spline at a time."""
        low_guid = int(low_guid)
        player_guid = int(player_guid or 0)
        current = self._idle_driver.get(low_guid)
        if current is not None:
            driver, until = current
            if driver != player_guid and until > now:
                return False
        self._idle_driver[low_guid] = (
            player_guid, float(now) + max(0.2, float(lease)))
        return True

    def forget_idle(self, low_guid: int) -> None:
        low_guid = int(low_guid)
        self._idle.pop(low_guid, None)
        self._idle_driver.pop(low_guid, None)

    def engage(self, low_guid: int, spawn, player_guid: int,
               start_pos=None) -> dict:
        """Return the shared state, creating it and adding ``player_guid``."""
        low_guid = int(low_guid)
        player_guid = int(player_guid or 0)
        state = self._states.get(low_guid)
        now = time.monotonic()
        map_id = int(spawn["map"] or 0) if "map" in spawn.keys() else 0
        if state is None:
            state = {
                "guid": low_guid,
                "entry": int(spawn["entry"] or 0),
                "map": map_id,
                "hostile": True,
                "pos": start_pos,
                "facing": None,
                "face_until": 0.0,
                "chase_until": 0.0,
                "last_swing": now - 1.0,
                "ai": None,
                "threat": 0.0,
                "threat_table": {},
                "threat_kind": {},
                "target_guid": 0,
                "tagger_guid": player_guid,
                "taunt_target": 0,
                "taunt_until": 0.0,
                "auras": [],
                "diminishing": {},
                "fleeing": False,
                "flee_until": 0.0,
                "last_mana_regen": now,
                "last_power_update": now,
            }
            self._states[low_guid] = state
        else:
            state["entry"] = int(spawn["entry"] or state.get("entry") or 0)
            state["map"] = map_id or int(state.get("map") or 0)
            if start_pos is not None and state.get("pos") is None:
                state["pos"] = start_pos
        state.setdefault("tagger_guid", player_guid)
        state.setdefault("auras", [])
        state.setdefault("diminishing", {})
        state.setdefault("threat_kind", {})
        table = state.setdefault("threat_table", {})
        table.setdefault(player_guid, 0.0)
        self._attach(player_guid, state)
        self.select_target(state, now)
        return state

    def forget(self, low_guid: int) -> Optional[dict]:
        low_guid = int(low_guid)
        state = self._states.pop(low_guid, None)
        for session in self._sessions():
            forget = getattr(session, "_forget_shared_creature", None)
            if forget is not None:
                forget(low_guid)
        return state

    def detach_session(self, player_guid: int) -> None:
        """Remove a logging-out player from every creature's threat table."""
        player_guid = int(player_guid or 0)
        if not player_guid:
            return
        for low_guid, state in list(self._states.items()):
            table = state.setdefault("threat_table", {})
            table.pop(player_guid, None)
            if not table:
                self.forget(low_guid)
                continue
            if int(state.get("target_guid") or 0) == player_guid:
                state["target_guid"] = 0
            self.select_target(state)
            if not int(state.get("target_guid") or 0):
                self.forget(low_guid)

    # -- threat / target selection -------------------------------------
    def add_threat(self, low_guid: int, player_guid: int,
                   amount: float, kind: str = "melee") -> Optional[int]:
        low_guid = int(low_guid)
        player_guid = int(player_guid or 0)
        state = self._states.get(low_guid)
        if state is None or not player_guid:
            return None
        table = state.setdefault("threat_table", {})
        new_amount = float(table.get(player_guid, 0.0)) + float(amount)
        table[player_guid] = max(0.0, new_amount)
        state.setdefault("threat_kind", {})[player_guid] = (
            "ranged" if kind in ("ranged", "spell") else "melee")
        state["threat"] = max(
            [float(value) for value in table.values()] or [0.0])
        self._attach(player_guid, state)
        return self.select_target(state)

    def modify_threat(self, low_guid: int, player_guid: int,
                      amount: float) -> Optional[int]:
        return self.add_threat(low_guid, player_guid, amount)

    def modify_threat_percent(self, low_guid: int, player_guid: int,
                              percent: float) -> Optional[int]:
        state = self._states.get(int(low_guid))
        if state is None:
            return None
        table = state.setdefault("threat_table", {})
        player_guid = int(player_guid or 0)
        if player_guid not in table:
            return None
        table[player_guid] = max(
            0.0, float(table[player_guid]) * (100.0 + float(percent)) / 100.0)
        state["threat"] = max(
            [float(value) for value in table.values()] or [0.0])
        return self.select_target(state)

    def healing_threat(self, healer_guid: int, healed_guid: int,
                       amount: float) -> None:
        """Healing adds half its effective amount as threat to engaged mobs."""
        healer_guid = int(healer_guid or 0)
        healed_guid = int(healed_guid or 0)
        if not healer_guid or amount <= 0:
            return
        for low_guid, state in list(self._states.items()):
            table = state.setdefault("threat_table", {})
            if healed_guid not in table and healer_guid not in table:
                continue
            # The healed player is the one the creature is fighting; the
            # healer takes the threat (self-heals credit themselves).
            self.add_threat(low_guid, healer_guid, float(amount) * 0.5,
                            kind="spell")

    def taunt(self, low_guid: int, player_guid: int, duration: float = 3.0):
        state = self._states.get(int(low_guid))
        if state is None:
            return
        state["taunt_target"] = int(player_guid or 0)
        state["taunt_until"] = time.monotonic() + max(0.0, float(duration))
        self.select_target(state)

    def select_target(self, state: dict, now: Optional[float] = None) -> int:
        """Pick the highest-threat living player, respecting an active taunt."""
        now = time.monotonic() if now is None else float(now)
        table = state.setdefault("threat_table", {})
        map_id = int(state.get("map") or 0)
        eligible = {
            int(guid): float(threat)
            for guid, threat in table.items()
            if guid and self.player_alive(int(guid), map_id)
        }
        if not eligible:
            state["target_guid"] = 0
            return 0

        current = int(state.get("target_guid") or 0)
        taunt_target = int(state.get("taunt_target") or 0)
        if (taunt_target and taunt_target in eligible
                and float(state.get("taunt_until") or 0.0) > now):
            chosen = taunt_target
        else:
            top_guid = max(eligible, key=lambda guid: eligible[guid])
            top_kind = state.get("threat_kind", {}).get(top_guid, "melee")
            factor = (TARGET_SWITCH_FACTOR_RANGED
                      if top_kind == "ranged" else TARGET_SWITCH_FACTOR)
            if current not in eligible:
                chosen = top_guid
            elif top_guid != current \
                    and eligible[top_guid] > eligible[current] * factor:
                chosen = top_guid
            else:
                chosen = current

        state["target_guid"] = chosen
        state["threat"] = max(eligible.values())
        self._attach(chosen, state)
        return chosen

    def clear_target(self, low_guid: int) -> None:
        state = self._states.get(int(low_guid))
        if state is not None:
            state["target_guid"] = 0
