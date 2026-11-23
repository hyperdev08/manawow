"""Creature wander and waypoint movement generators."""
from __future__ import annotations

import math
import random
import sqlite3

from src.protocol.opcodes import Opcodes
from src.world import creature
from src.world import object_update

from .constants import (
    IDLE_MOVE_MAX_DURATION_MS,
    IDLE_MOVE_MIN_DURATION_MS,
    IDLE_WANDER_DEFAULT_RADIUS,
    IDLE_WANDER_DELAY_MAX,
    IDLE_WANDER_DELAY_MIN,
    IDLE_WANDER_MAX_MOVES,
    IDLE_WANDER_RETRY,
    UNIT_FLAG_DISABLE_MOVE,
)

class CreatureMovementMixin:
    async def _face_creature_at_player(self, low_guid: int, state: dict, spawn,
                                       here, player, now: float) -> None:
        """Turn a stationary creature towards the player.

        When the creature is in melee range it no longer sends movement
        splines, so the client would keep the orientation from its spawn row.
        Sending a ``MONSTER_MOVE`` facing-angle spline updates it in place.
        """
        if now < float(state.get("chase_until") or 0.0):
            return                       # still animating the last chase spline
        if now < float(state.get("face_until") or 0.0):
            return                       # throttled
        angle = math.atan2(player[1] - here[1], player[0] - here[0])
        last = state.get("facing")
        if last is None:
            last = float(spawn["o"] or 0.0) if "o" in spawn.keys() else 0.0
            state["facing"] = last
        # Shortest signed difference, so a turn across the +/-pi boundary still
        # counts as a small angle.
        delta = (angle - float(last) + math.pi) % (2.0 * math.pi) - math.pi
        if abs(delta) < 0.15:
            return
        state["facing"] = angle
        state["face_until"] = now + 0.4
        await self._send_creature_packet(
            int(low_guid), Opcodes.SMSG_MONSTER_MOVE,
            object_update.build_monster_face(
                self._creature_object_guid(int(low_guid), int(spawn["entry"])),
                here[0], here[1], here[2], float(last), angle,
                move_id=self._next_move_id(),
            ),
        )

    def _creature_waypoint_path(self, spawn) -> list:
        """Waypoint rows for a spawn, or ``[]`` when it has no path.

        ``creature_movement`` is keyed by the spawn GUID; a shared path lives in
        ``creature_movement_template`` under the entry and the template's
        ``MovementTemplateId``.  Neither table exists in a world dump that was
        never imported, so a missing path simply means "no waypoints".
        """
        try:
            path = creature.get_creature_movement(
                self.config.database.world_path, int(spawn["guid"]))
            if path:
                return path
            return creature.get_creature_movement_template(
                self.config.database.world_path, int(spawn["entry"]),
                int(spawn["movement_info_id"] or 0))
        except sqlite3.Error:
            return []

    def _creature_movement_plan(self, spawn) -> tuple[str, list]:
        """Pick ``(kind, waypoint_path)`` for a spawn.

        The per-spawn ``MovementType`` overrides the template value, which in
        turn overrides the heuristic used for databases imported before those
        columns were read.  A waypoint path is only consulted for waypoint (or
        unknown) movement types, exactly like ``Creature::LoadFromDB`` picks the
        movement generator.
        """
        if int(spawn["dead_since"] or 0) > 0:
            return "idle", []
        if int(spawn["unit_flags"] or 0) & UNIT_FLAG_DISABLE_MOVE:
            return "idle", []
        movement_type = int(spawn["spawn_movement_type"])
        if movement_type < 0:
            movement_type = int(spawn["template_movement_type"] or 0)
        if movement_type == 0:
            return "idle", []
        if movement_type == 1:
            return "wander", []
        path = self._creature_waypoint_path(spawn)
        if movement_type == 2:
            return ("waypoint", path) if path else ("idle", [])
        # Unknown (-1): a path still means waypoints; otherwise NPCs stay put
        # and wild creatures roam a little around their spawn point.
        if path:
            return "waypoint", path
        if int(spawn["npc_flags"] or 0):
            return "idle", []
        return "wander", []

    def _creature_idle_state(self, low_guid: int, spawn, now: float) -> dict:
        """Return (and lazily build) the shared idle state for a creature."""
        def build() -> dict:
            kind, path = self._creature_movement_plan(spawn)
            radius = float(spawn["spawndist"] or 0.0)
            if radius <= 0.0:
                radius = IDLE_WANDER_DEFAULT_RADIUS
            return {
                "kind": kind,
                "home": (float(spawn["x"]), float(spawn["y"]),
                         float(spawn["z"])),
                "pos": None,
                "facing": float(spawn["o"] or 0.0),
                "radius": max(0.5, radius),
                "speed": max(0.5, float(spawn["speed_walk"] or 2.5)),
                "next_move": now + random.uniform(0.0, 2.0),
                "move_until": 0.0,
                "moves_left": 1,
                "path": path,
                "path_index": 0,
                "wait_until": 0.0,
            }

        manager = (self._creature_combat_manager()
                   if hasattr(self, "_creature_combat_manager") else None)
        if manager is not None:
            state = manager.idle_state(int(low_guid), build)
        else:
            idle = self._get_combat_state().setdefault("idle", {})
            state = idle.get(int(low_guid))
            if state is None:
                state = build()
                idle[int(low_guid)] = state
        self._get_combat_state().setdefault("idle", {})[int(low_guid)] = state
        return state

    async def _send_creature_idle_move(self, low_guid: int, spawn, state: dict,
                                       here, dest, now: float) -> bool:
        """Send one walk spline for idle movement.  Returns False if too short."""
        dx = float(dest[0]) - here[0]
        dy = float(dest[1]) - here[1]
        dz = float(dest[2]) - here[2]
        distance = (dx * dx + dy * dy + dz * dz) ** 0.5
        if distance < 0.1:
            return False
        speed = float(state["speed"]) or 2.5
        if hasattr(self, "_creature_speed_mult"):
            speed *= max(0.1, self._creature_speed_mult(int(low_guid)))

        # Idle movement (waypoints and wander) normally goes straight across
        # open terrain.  Only pay for an A* query when the straight segment is
        # actually obstructed, which keeps dozens of ambient NPCs cheap.
        path = None
        if distance > 2.0 and hasattr(self, "_find_world_path"):
            eye_a = (here[0], here[1], here[2] + 0.9)
            eye_b = (float(dest[0]), float(dest[1]), float(dest[2]) + 0.9)
            if not self._has_line_of_sight(eye_a, eye_b):
                path = self._find_world_path(here, dest)
        if path and len(path) > 1:
            max_travel = speed * IDLE_MOVE_MAX_DURATION_MS / 1000.0
            points = self._path_prefix(here, path, max_travel)
            if not points:
                return False
            travel = 0.0
            cursor = here
            for point in points:
                travel += self._distance(cursor, point)
                cursor = point
            if travel < 0.1:
                return False
            duration_ms = int(travel / speed * 1000.0)
            duration_ms = max(IDLE_MOVE_MIN_DURATION_MS,
                              min(duration_ms, IDLE_MOVE_MAX_DURATION_MS))
            target = points[-1]
        else:
            duration_ms = int(distance / speed * 1000.0)
            duration_ms = max(IDLE_MOVE_MIN_DURATION_MS,
                              min(duration_ms, IDLE_MOVE_MAX_DURATION_MS))
            target = (float(dest[0]), float(dest[1]), float(dest[2]))
            points = [target]

        facing = math.atan2(target[1] - here[1], target[0] - here[0])
        state["pos"] = (float(target[0]), float(target[1]), float(target[2]))
        state["facing"] = facing
        state["move_until"] = now + duration_ms / 1000.0
        await self._send_creature_packet(
            int(low_guid), Opcodes.SMSG_MONSTER_MOVE,
            object_update.build_monster_move_path(
                self._creature_object_guid(int(low_guid), int(spawn["entry"])),
                here[0], here[1], here[2], facing, points,
                duration_ms=duration_ms,
                move_id=self._next_move_id(),
            ),
        )
        return True

    async def _creature_waypoint_tick(self, low_guid: int, spawn, state: dict,
                                      now: float) -> None:
        """Walk one node of a waypoint path, waiting ``WaitTime`` on arrival."""
        path = state["path"]
        if not path:
            state["kind"] = "idle"
            return
        if now < float(state["wait_until"] or 0.0):
            return
        if state["pos"] is None:
            state["pos"] = state["home"]
        here = tuple(state["pos"])
        index = int(state["path_index"]) % len(path)
        node = path[index]
        dest = (float(node["position_x"]), float(node["position_y"]),
                float(node["position_z"]))
        if self._distance(here, dest) < 0.2:
            # Reached the node: pause for its WaitTime, then target the next.
            wait_ms = max(0, int(node["wait_time"] or 0))
            state["wait_until"] = now + wait_ms / 1000.0
            state["path_index"] = (index + 1) % len(path)
            return
        if not await self._send_creature_idle_move(
                low_guid, spawn, state, here, dest, now):
            state["wait_until"] = now + IDLE_WANDER_RETRY

    async def _creature_idle_tick(self, low_guid: int, spawn, now: float) -> None:
        """One movement tick for a creature that is not fighting this player."""
        if "summoned_by" in spawn.keys() and int(spawn["summoned_by"] or 0):
            return                       # pets follow their owner in _summon_tick
        if hasattr(self, "_creature_aura_mods"):
            mods = self._creature_aura_mods(int(low_guid))
            if (mods["rooted"] or mods["stunned"] or mods["feared"]
                    or mods["confused"] or mods["charmed"]):
                return
        combat = self._get_combat_state()
        if int(low_guid) in combat["aggro"]:
            return
        manager = (self._creature_combat_manager()
                   if hasattr(self, "_creature_combat_manager") else None)
        if manager is not None and manager.is_engaged(int(low_guid)):
            return
        if manager is not None and not manager.claim_idle(
                int(low_guid), int(self.player_guid or 0), now):
            return
        if int(spawn["dead_since"] or 0) > 0:
            combat["idle"].pop(int(low_guid), None)
            return
        settle = combat.setdefault("idle_settle", {})
        until = settle.get(int(low_guid))
        if until is not None:
            if now < until:
                return
            settle.pop(int(low_guid), None)
        state = self._creature_idle_state(int(low_guid), spawn, now)
        if state["kind"] == "idle":
            return
        if now < float(state["move_until"] or 0.0):
            return
        if state["kind"] == "waypoint":
            await self._creature_waypoint_tick(int(low_guid), spawn, state, now)
            return
        if now < float(state["next_move"] or 0.0):
            return

        # Wander: pick a random destination inside the spawn radius, mirroring
        # ``PathFinder::ComputePathToRandomPoint`` (straight line here because
        # this server has no terrain/pathfinding data).
        if state["pos"] is None:
            state["pos"] = state["home"]
        here = tuple(state["pos"])
        home = state["home"]
        radius = float(state["radius"])
        dest = None
        # Prefer a real navmesh point: it is reachable, lies on a walkable
        # surface and already has the right height.  The random-circle fallback
        # is only used before the client data cache is populated.
        for _ in range(6):
            candidate = (self._random_world_point(home, radius)
                         if hasattr(self, "_random_world_point") else None)
            if candidate is None:
                break
            if self._distance(here, candidate) < 0.5:
                continue
            dest = candidate
            break
        if dest is None:
            for _ in range(6):
                angle = random.uniform(0.0, 2.0 * math.pi)
                distance = random.uniform(radius * 0.35, radius)
                dx = math.cos(angle) * distance
                dy = math.sin(angle) * distance
                candidate = (home[0] + dx, home[1] + dy, home[2])
                # ``ComputePathToRandomPoint`` picks a point around the respawn
                # coordinate, not around the current position, so the creature
                # never drifts outside ``spawndist``.
                if self._distance(here, candidate) < 0.5:
                    continue
                dest = candidate
                break
        if dest is None or not await self._send_creature_idle_move(
                int(low_guid), spawn, state, here, dest, now):
            state["next_move"] = now + IDLE_WANDER_RETRY
            return
        # Up to three consecutive steps, then a 3-10 s pause.
        if int(state["moves_left"]) > 1:
            state["moves_left"] = int(state["moves_left"]) - 1
        else:
            state["moves_left"] = random.randint(1, IDLE_WANDER_MAX_MOVES)
            state["next_move"] = state["move_until"] + random.uniform(
                IDLE_WANDER_DELAY_MIN, IDLE_WANDER_DELAY_MAX)

    async def _creature_idle_tick_all(self, now: float) -> None:
        """Advance idle movement for every visible creature."""
        cache = getattr(self, "_creature_spawn_cache", {})
        if not cache:
            return
        aggro = self._get_combat_state()["aggro"]
        for low_guid in list(cache):
            if low_guid in aggro:
                continue
            spawn = cache.get(low_guid)
            if spawn is None:
                continue
            await self._creature_idle_tick(low_guid, spawn, now)
