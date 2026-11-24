"""Game objects: visibility, queries, use, loot and respawn."""
from __future__ import annotations

import asyncio
import random
import struct
import time

from src.protocol.opcodes import Opcodes
from src.world import gameobject
from src.world import login_packets
from src.world import movement
from src.world import object_update
from src.world import quests as Q
from src.world import update_fields as F

# Shared fallback for sessions created without a world server (unit tests).
_FALLBACK_RUNTIME = gameobject.GameObjectRuntime()

# ``UNIT_FLAG_LOOTING``: TrinityCore ``Player::SendLoot`` sets it while a loot
# window is open.  Without it the client shows an object's loot but never
# sends the click that takes an item.
UNIT_FLAG_LOOTING = 0x00000400


class GameObjectsMixin:
    # ------------------------------------------------------------------
    # Shared state
    # ------------------------------------------------------------------
    def _go_runtime(self) -> gameobject.GameObjectRuntime:
        runtime = getattr(getattr(self, "server", None), "gameobjects", None)
        return runtime if runtime is not None else _FALLBACK_RUNTIME

    def _go_sessions(self, low_guid: int) -> list:
        """In-world sessions (this one included) that currently see the object."""
        sessions = list(getattr(getattr(self, "server", None), "sessions", ()) or ())
        if self not in sessions:
            sessions.append(self)
        return [s for s in sessions
                if getattr(s, "in_world", False)
                and int(low_guid) in getattr(s, "_visible_gameobjects", {})]

    def _go_state(self, spawn) -> int:
        state = self._go_runtime().state(int(spawn["guid"]))
        return gameobject.initial_state(spawn) if state is None else state

    def _go_dynamic(self, spawn) -> int:
        """Per-player ``GAMEOBJECT_DYNAMIC`` (TrinityCore ``BuildValuesUpdate``).

        Quest chests and goobers become clickable and sparkle for players who
        need them; a generic quest object only sparkles.
        """
        path = self.config.database.world_path
        if not gameobject.is_quest_object(spawn, path):
            return 0
        if not self._go_activates_for_quest(spawn):
            return 0
        go_type = int(spawn["type"])
        if go_type == gameobject.GAMEOBJECT_TYPE_GENERIC:
            return gameobject.GO_DYNFLAG_LO_SPARKLE
        if go_type == gameobject.GAMEOBJECT_TYPE_QUESTGIVER:
            # Wanted posters carry GO_FLAG_INTERACT_COND: clickable only while
            # they have something for the player (no sparkle).
            return gameobject.GO_DYNFLAG_LO_ACTIVATE
        return gameobject.GO_DYNFLAG_LO_ACTIVATE | gameobject.GO_DYNFLAG_LO_SPARKLE

    def _quest_incomplete(self, quest_id: int) -> bool:
        if not quest_id or not hasattr(self, "_quest_log_map"):
            return False
        state = self._quest_log_map().get(int(quest_id))
        return state is not None and int(state["status"]) == Q.QUEST_STATUS_INCOMPLETE

    def _go_activates_for_quest(self, spawn) -> bool:
        """``GameObject::ActivateToQuest``."""
        go_type = int(spawn["type"])
        if go_type == gameobject.GAMEOBJECT_TYPE_QUESTGIVER:
            # Available, in progress or ready to hand in (above the grey "!").
            return self._questgiver_status(
                int(spawn["entry"]), Q.GIVER_GAMEOBJECT) > Q.DIALOG_STATUS_UNAVAILABLE
        if go_type in (gameobject.GAMEOBJECT_TYPE_GOOBER, gameobject.GAMEOBJECT_TYPE_GENERIC):
            return self._quest_incomplete(gameobject.object_quest_id(spawn))
        quest_id = gameobject.chest_quest_id(spawn)
        if quest_id and hasattr(self, "_quest_log_map"):
            state = self._quest_log_map().get(quest_id)
            if state is not None and int(state["status"]) == Q.QUEST_STATUS_INCOMPLETE:
                return True
        needs = getattr(self, "_quest_needs_item", None)
        if needs is None:
            return False
        path = self.config.database.world_path
        return any(needs(item) for item in
                   gameobject.quest_loot_items(path, gameobject.loot_id(spawn)))

    # ------------------------------------------------------------------
    # Visibility
    # ------------------------------------------------------------------
    def _gameobject_scan_point(self) -> tuple[int, float, float, float] | None:
        if not self.character:
            return None
        return (
            int(self.character["map"]),
            float(self.character["x"]),
            float(self.character["y"]),
            float(self.character["z"]),
        )

    async def _gameobjects_near(self, point) -> list[dict]:
        map_id, x, y, z = point
        path = self.config.database.world_path
        if not gameobject.is_map_loaded(path, map_id):
            # The first visit to a map reads all of its spawns; do it off the
            # event loop so other players do not freeze while it loads.
            await asyncio.to_thread(gameobject.load_map, path, map_id)
        runtime = self._go_runtime()
        if runtime.pools is None:
            pools = await asyncio.to_thread(gameobject.GameObjectPools.load, path)
            if runtime.pools is None:
                runtime.pools = pools
        now = time.monotonic()
        return [s for s in gameobject.gameobjects_near(path, map_id, x, y, z)
                if runtime.is_spawned(int(s["guid"]), now)]

    def _remember_gameobjects(self, spawns) -> None:
        self._visible_gameobjects = {
            int(s["guid"]): gameobject.gameobject_guid(s["guid"], s["entry"])
            for s in spawns
        }
        self._gameobject_spawn_cache = {int(s["guid"]): s for s in spawns}
        self._gameobject_dynamic = {int(s["guid"]): self._go_dynamic(s) for s in spawns}

    async def send_gameobject_spawns(self) -> list[bytes]:
        """Create blocks for the objects around the player (login burst)."""
        point = self._gameobject_scan_point()
        if point is None:
            return []
        try:
            spawns = await self._gameobjects_near(point)
        except Exception as exc:
            self.log.warning(f"Could not load game objects: {exc}")
            return []
        self._remember_gameobjects(spawns)
        self._gameobject_scan_pos = point
        if spawns:
            self.log.info(
                f"Adding {len(spawns)} game object(s) to "
                f"'{self.character['name']}' on map {point[0]}"
            )
        return [
            gameobject.build_gameobject_create_block(
                s, self._go_state(s), self._gameobject_dynamic[int(s["guid"])])
            for s in spawns
        ]

    async def refresh_gameobject_visibility(self, force: bool = False) -> None:
        """Create objects that came into range and destroy those that left."""
        # Nothing may reach the client before the login burst has created the
        # player: the periodic tick can run while that burst is being built.
        if (not self.in_world or not self.character
                or not getattr(self, "world_entry_complete", False)):
            return
        point = self._gameobject_scan_point()
        if point is None:
            return
        last = self._gameobject_scan_pos
        if not force and last is not None and last[0] == point[0]:
            dx, dy, dz = point[1] - last[1], point[2] - last[2], point[3] - last[3]
            if dx * dx + dy * dy + dz * dz < gameobject.GAMEOBJECT_RESCAN_DISTANCE ** 2:
                return
        # Claim the scan before awaiting a map load so the heartbeats that
        # arrive meanwhile do not start the same scan again.
        self._gameobject_scan_pos = point
        try:
            spawns = await self._gameobjects_near(point)
        except Exception as exc:
            self.log.warning(f"Could not load game objects: {exc}")
            return
        if not self.in_world or not self.character:
            return
        current = {int(s["guid"]): s for s in spawns}
        previous = self._visible_gameobjects
        old_dynamic = getattr(self, "_gameobject_dynamic", {})

        new = [s for guid, s in current.items() if guid not in previous]
        packet = gameobject.build_gameobject_update(
            new, state_of=self._go_state, dynamic_of=self._go_dynamic)
        if packet is not None:
            await self.send_packet(*packet)
        gone = [guid for guid in previous if guid not in current]
        for guid in gone:
            await self.send_packet(
                Opcodes.SMSG_DESTROY_OBJECT,
                login_packets.build_destroy_object(previous[guid]),
            )
        if new or gone:
            self.log.info(
                f"Visibility: +{len(new)} -{len(gone)} game object(s) for "
                f"'{self.character['name']}'"
            )
        self._remember_gameobjects(spawns)
        # Objects that stayed keep the flags the client already has.
        for guid in current:
            if guid in previous and guid in old_dynamic:
                self._gameobject_dynamic[guid] = old_dynamic[guid]

    async def destroy_visible_gameobjects(self) -> None:
        """Remove every object from the client (world rebuild)."""
        for object_guid in list(self._visible_gameobjects.values()):
            await self.send_packet(
                Opcodes.SMSG_DESTROY_OBJECT,
                login_packets.build_destroy_object(object_guid),
            )
        self.forget_gameobjects()

    def forget_gameobjects(self) -> None:
        self._visible_gameobjects = {}
        self._gameobject_spawn_cache = {}
        self._gameobject_dynamic = {}
        self._gameobject_scan_pos = None

    async def refresh_gameobject_quest_flags(self) -> None:
        """Re-send ``GAMEOBJECT_DYNAMIC`` where the player's quests changed it."""
        if not self.in_world:
            return
        cache = getattr(self, "_gameobject_spawn_cache", {})
        dynamic = getattr(self, "_gameobject_dynamic", {})
        for low_guid, spawn in list(cache.items()):
            value = self._go_dynamic(spawn)
            if dynamic.get(low_guid, 0) == value:
                continue
            dynamic[low_guid] = value
            await self.send_packet(*object_update.build_values_update(
                self._visible_gameobjects[low_guid], F.GAMEOBJECT_END,
                {F.GAMEOBJECT_DYNAMIC: value}))

    # ------------------------------------------------------------------
    # World-wide changes
    # ------------------------------------------------------------------
    async def _broadcast_gameobject_state(self, spawn, state: int) -> None:
        low_guid = int(spawn["guid"])
        value = ((int(state) & 0xFF) | ((int(spawn["type"]) & 0xFF) << 8)
                 | ((int(spawn["anim_progress"]) & 0xFF) << 24))
        for session in self._go_sessions(low_guid):
            await session.send_packet(*object_update.build_values_update(
                session._visible_gameobjects[low_guid], F.GAMEOBJECT_END,
                {F.GAMEOBJECT_BYTES_1: value}))

    async def _despawn_gameobject(self, spawn) -> None:
        """Remove a used object from every client until its respawn time."""
        low_guid = int(spawn["guid"])
        delay = gameobject.respawn_delay(spawn)
        self._go_runtime().despawn(low_guid, delay)
        for session in self._go_sessions(low_guid):
            object_guid = session._visible_gameobjects.pop(low_guid)
            getattr(session, "_gameobject_spawn_cache", {}).pop(low_guid, None)
            getattr(session, "_gameobject_dynamic", {}).pop(low_guid, None)
            await session.send_packet(
                Opcodes.SMSG_DESTROY_OBJECT,
                login_packets.build_destroy_object(object_guid),
            )
        self.log.info(f"Game object {low_guid} ({spawn['name']}) despawned for {delay}s")

    async def _gameobject_tick(self, now: float) -> None:
        """Close doors whose timer ran out and bring respawned objects back."""
        runtime = self._go_runtime()
        for low_guid in runtime.respawn_pools(now):
            self.log.debug(f"Pooled game object {low_guid} spawned")
        for low_guid in runtime.expired_states(now):
            for session in self._go_sessions(low_guid):
                spawn = session._gameobject_spawn_cache.get(low_guid)
                if spawn is not None:
                    await session.send_packet(*object_update.build_values_update(
                        session._visible_gameobjects[low_guid], F.GAMEOBJECT_END,
                        {F.GAMEOBJECT_BYTES_1: (
                            (gameobject.initial_state(spawn) & 0xFF)
                            | ((int(spawn["type"]) & 0xFF) << 8)
                            | ((int(spawn["anim_progress"]) & 0xFF) << 24))}))
        # A respawned object needs no movement to reappear.
        if self.in_world and self.character and getattr(self, "world_entry_complete", False):
            await self.refresh_gameobject_visibility(force=True)

    # ------------------------------------------------------------------
    # Using objects
    # ------------------------------------------------------------------
    def _usable_gameobject(self, guid: int, max_distance: float | None = None):
        """The spawn behind ``guid`` if this player can reach it, else ``None``."""
        if not gameobject.is_gameobject_guid(guid):
            return None
        low_guid = gameobject.gameobject_low_guid(guid)
        if self._visible_gameobjects.get(low_guid) != int(guid):
            return None
        spawn = self._gameobject_spawn_cache.get(low_guid)
        if spawn is None or not self._go_runtime().is_spawned(low_guid):
            return None
        limit = gameobject.GAMEOBJECT_USE_DISTANCE if max_distance is None else max_distance
        position = (float(spawn["x"]), float(spawn["y"]), float(spawn["z"]))
        if self._distance(self._player_position(), position) > limit:
            return None
        return spawn

    async def handle_gameobject_use(self, data: bytes):
        """``CMSG_GAMEOBJ_USE``: the player clicked an object without a lock."""
        if not self.in_world or not self.character or len(data) < 8:
            return
        guid = struct.unpack_from("<Q", data, 0)[0]
        spawn = self._usable_gameobject(guid)
        if spawn is None:
            self.log.debug(f"Game object use for {guid:#x} out of reach")
            return
        if gameobject.lock_id(spawn):
            # The client casts an opening spell for locked objects; a bare use
            # packet for one is not allowed to bypass the lock.
            self.log.debug(f"Game object {spawn['name']} is locked")
            return
        await self._use_gameobject(guid, spawn)

    async def _open_lock(self, target_guid: int, spell_id: int) -> bool:
        """``SPELL_EFFECT_OPEN_LOCK`` on an object (Opening, Herb Gathering...).

        The client only casts the spell after checking the lock's skill
        requirement against its own ``Lock.dbc``; the server has no copy of
        that table yet, so it trusts that check.
        """
        spawn = self._usable_gameobject(target_guid)
        if spawn is None:
            return False
        self.log.info(f"'{self.character['name']}' opens {spawn['name']} with spell {spell_id}")
        await self._use_gameobject(target_guid, spawn)
        return True

    async def _use_gameobject(self, guid: int, spawn) -> None:
        go_type = int(spawn["type"])
        if go_type in (gameobject.GAMEOBJECT_TYPE_DOOR, gameobject.GAMEOBJECT_TYPE_BUTTON):
            await self._toggle_gameobject(spawn)
        elif go_type == gameobject.GAMEOBJECT_TYPE_CHEST:
            await self._open_gameobject_loot(guid, spawn)
        elif go_type == gameobject.GAMEOBJECT_TYPE_CHAIR:
            await self._sit_on_chair(spawn)
        elif go_type == gameobject.GAMEOBJECT_TYPE_TEXT:
            # The client asks for the pages itself (CMSG_PAGE_TEXT_QUERY).
            await self.send_packet(Opcodes.SMSG_GAMEOBJECT_PAGETEXT,
                                   struct.pack("<Q", int(guid)))
        elif go_type == gameobject.GAMEOBJECT_TYPE_CAMERA:
            cinematic = gameobject.camera_cinematic(spawn)
            if cinematic:
                await self.send_packet(Opcodes.SMSG_TRIGGER_CINEMATIC,
                                       struct.pack("<I", cinematic))
        elif go_type == gameobject.GAMEOBJECT_TYPE_MAILBOX:
            pass                        # the client opens the frame itself
        elif go_type == gameobject.GAMEOBJECT_TYPE_QUESTGIVER:
            await self._open_quest_object(guid, spawn)
        elif go_type == gameobject.GAMEOBJECT_TYPE_GOOBER:
            await self._use_goober(guid, spawn)
        else:
            self.log.debug(f"Using game object type {go_type} ({spawn['name']}) "
                           "is not implemented yet")

    async def _toggle_gameobject(self, spawn) -> None:
        """Open a closed door/button or close an open one (``GameObject::UseDoorOrButton``)."""
        low_guid = int(spawn["guid"])
        current = self._go_state(spawn)
        new_state = (gameobject.GO_STATE_ACTIVE if current != gameobject.GO_STATE_ACTIVE
                     else gameobject.GO_STATE_READY)
        runtime = self._go_runtime()
        if new_state == gameobject.initial_state(spawn):
            runtime.states.pop(low_guid, None)
        else:
            runtime.set_state(low_guid, new_state, gameobject.auto_close_seconds(spawn))
        await self._broadcast_gameobject_state(spawn, new_state)

    async def _open_quest_object(self, guid: int, spawn) -> None:
        """A wanted poster or quest book: its quest list (``SendPreparedQuest``)."""
        giver = dict(spawn, giver_kind=Q.GIVER_GAMEOBJECT)
        if not await self._send_quest_menu(guid, giver):
            await self.send_packet(Opcodes.SMSG_GOSSIP_COMPLETE, b"")

    async def _use_goober(self, guid: int, spawn) -> None:
        """``GameObject::Use`` for GOOBER: pages, quest credit, animation, spell."""
        page = gameobject.goober_page(spawn)
        if page:
            await self.send_packet(Opcodes.SMSG_GAMEOBJECT_PAGETEXT, struct.pack("<Q", int(guid)))
        quest_id = gameobject.object_quest_id(spawn)
        if quest_id and not self._quest_incomplete(quest_id):
            return                       # the object only works for its quest
        if hasattr(self, "_quest_on_gameobject_used"):
            await self._quest_on_gameobject_used(int(guid), int(spawn["entry"]))
        low_guid = int(spawn["guid"])
        anim = gameobject.goober_custom_anim(spawn)
        if anim:
            for session in self._go_sessions(low_guid):
                await session.send_packet(
                    Opcodes.SMSG_GAMEOBJECT_CUSTOM_ANIM,
                    struct.pack("<QI", int(guid), anim))
        else:
            self._go_runtime().set_state(low_guid, gameobject.GO_STATE_ACTIVE,
                                         gameobject.goober_reset_seconds(spawn))
            await self._broadcast_gameobject_state(spawn, gameobject.GO_STATE_ACTIVE)
        spell = gameobject.goober_spell(spawn)
        if spell and hasattr(self, "_cast_triggered"):
            try:
                await self._cast_triggered(spell, int(self.player_guid))
            except Exception as exc:          # unsupported effects must not break use
                self.log.warning(f"Goober {spawn['name']} spell {spell} failed: {exc}")
        if gameobject.goober_consumable(spawn):
            await self._despawn_gameobject(spawn)

    async def _sit_on_chair(self, spawn) -> None:
        """Move onto the nearest seat and sit (``GameObject::Use`` CHAIR)."""
        x, y = gameobject.chair_seat(spawn, float(self.character["x"]),
                                     float(self.character["y"]))
        z, o = float(spawn["z"]), float(spawn["o"] or 0.0)
        await self._teleport_nearby(x, y, z, o)
        state = gameobject.chair_stand_state(spawn)
        await self._set_stand_state(state)
        # Unit::SetStandState tells the player's own client explicitly.
        await self.send_packet(Opcodes.SMSG_STANDSTATE_UPDATE, struct.pack("<B", state))

    async def _teleport_nearby(self, x: float, y: float, z: float, o: float) -> None:
        """Same-map teleport: ``MSG_MOVE_TELEPORT_ACK`` to the player, ``MSG_MOVE_TELEPORT`` to others."""
        self.character["x"], self.character["y"] = float(x), float(y)
        self.character["z"], self.character["o"] = float(z), float(o)
        info = movement.MovementInfo(time=self._server_tick_ms(), x=float(x), y=float(y),
                                     z=float(z), o=float(o))
        self.movement_info = info
        self._teleport_counter = getattr(self, "_teleport_counter", 0) + 1
        await self.send_packet(
            Opcodes.MSG_MOVE_TELEPORT_ACK,
            movement.pack_guid(int(self.player_guid))
            + struct.pack("<I", self._teleport_counter)
            + movement.build_movement_info(info))
        if hasattr(self, "send_packet_to_peers"):
            await self.send_packet_to_peers(
                Opcodes.MSG_MOVE_TELEPORT,
                movement.build_movement_packet(int(self.player_guid), info))
        await self.persist_position(force=True)

    async def handle_teleport_ack(self, data: bytes):
        """``MSG_MOVE_TELEPORT_ACK`` from the client: the near teleport is done."""
        self.log.debug("Near teleport acknowledged")

    async def handle_page_text_query(self, data: bytes):
        """``CMSG_PAGE_TEXT_QUERY``: ``uint32 pageID, uint64 guid``."""
        if len(data) < 4:
            return
        page_id = struct.unpack_from("<I", data, 0)[0]
        for payload in gameobject.build_page_text_responses(
                self.config.database.world_path, page_id):
            await self.send_packet(Opcodes.SMSG_PAGE_TEXT_QUERY_RESPONSE, payload)

    async def handle_get_mail_list(self, data: bytes):
        """``CMSG_GET_MAIL_LIST``: there is no mail system yet, the box is empty."""
        await self.send_packet(Opcodes.SMSG_MAIL_LIST_RESULT, struct.pack("<IB", 0, 0))

    async def handle_query_next_mail_time(self, data: bytes):
        """``MSG_QUERY_NEXT_MAIL_TIME``: no unread mail (``-DAY``, no senders)."""
        await self.send_packet(Opcodes.MSG_QUERY_NEXT_MAIL_TIME,
                               struct.pack("<fI", -86400.0, 0))

    # ------------------------------------------------------------------
    # Loot
    # ------------------------------------------------------------------
    async def _open_gameobject_loot(self, guid: int, spawn) -> None:
        low_guid = int(spawn["guid"])
        runtime = self._go_runtime()
        player = int(self.player_guid or 0)
        looter = runtime.looter.get(low_guid)
        if looter and looter != player:
            # Someone else has the window open (``GO_ACTIVATED`` for them).
            await self._send_loot_response(guid, [], 0)
            return
        # A quest chest only opens for a player it is activated for; rolling
        # its loot for anyone else would store an empty chest for the next
        # player who does need it.
        if (gameobject.is_quest_object(spawn, self.config.database.world_path)
                and not self._go_activates_for_quest(spawn)):
            await self._send_loot_response(guid, [], 0)
            return
        stored = runtime.stored_loot(low_guid)
        if stored is None:
            loot = gameobject.loot_id(spawn)
            items = self._generate_loot(0, loot, table="gameobject_loot_template") if loot else []
            min_gold, max_gold = int(spawn["min_gold"] or 0), int(spawn["max_gold"] or 0)
            money = random.randint(min_gold, max(min_gold, max_gold)) if max_gold > 0 else 0
            stored = {"items": items, "money": money}
            runtime.loot[low_guid] = stored
        runtime.looter[low_guid] = player
        await self._close_loot()
        combat = self._get_combat_state()
        combat["loot_target"] = int(guid)
        combat["loot_items"] = list(stored["items"])
        combat["loot_taken"] = []
        combat["loot_money"] = int(stored["money"])
        await self._send_loot_response(guid, combat["loot_items"], combat["loot_money"])
        await self._set_looting_flag(True)

    async def _set_looting_flag(self, looting: bool) -> None:
        """Toggle ``UNIT_FLAG_LOOTING`` on the player's own unit flags."""
        if not self.in_world or not self.character:
            return
        state = self._get_combat_state().setdefault("aura_state", {})
        current = int(state.get("flags", 0x00000008))     # PLAYER_CONTROLLED
        flags = current | UNIT_FLAG_LOOTING if looting else current & ~UNIT_FLAG_LOOTING
        if flags != current:
            state["flags"] = flags
            await self._send_player_fields({F.UNIT_FIELD_FLAGS: flags})

    def _persist_gameobject_loot(self, combat) -> None:
        low_guid = gameobject.gameobject_low_guid(combat["loot_target"])
        self._go_runtime().loot[low_guid] = {
            "items": self._remaining_loot(combat),
            "money": int(combat.get("loot_money") or 0),
        }

    async def _release_gameobject_loot(self, guid: int, emptied: bool) -> None:
        """The loot window closed (``Player::DoLootRelease`` for objects).

        A looted chest that is consumable, or has no restock timer, despawns
        until its respawn time; a reusable chest stays and refills later.  A
        chest with loot left stays as it is for the next player.
        """
        low_guid = gameobject.gameobject_low_guid(guid)
        runtime = self._go_runtime()
        if runtime.looter.get(low_guid) == int(self.player_guid or 0):
            runtime.looter.pop(low_guid, None)
        if not emptied:
            return
        spawn = self._gameobject_spawn_cache.get(low_guid)
        if spawn is None:
            runtime.loot.pop(low_guid, None)
            return
        restock = gameobject.restock_seconds(spawn)
        if gameobject.is_consumable(spawn) or restock <= 0:
            await self._despawn_gameobject(spawn)
        else:
            runtime.restock[low_guid] = time.monotonic() + restock

    # ------------------------------------------------------------------
    # Opcode handlers
    # ------------------------------------------------------------------
    async def handle_gameobject_query(self, data: bytes):
        """``CMSG_GAMEOBJECT_QUERY``: ``uint32 entry, uint64 guid``."""
        if len(data) < 4:
            return
        entry = struct.unpack_from("<I", data, 0)[0]
        template = gameobject.get_gameobject_template(
            self.config.database.world_path, entry)
        if template is None:
            self.log.debug(f"Game object query for unknown entry {entry}")
        await self.send_packet(
            Opcodes.SMSG_GAMEOBJECT_QUERY_RESPONSE,
            gameobject.build_gameobject_query_response(entry, template),
        )
