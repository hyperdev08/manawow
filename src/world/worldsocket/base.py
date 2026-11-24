"""Connection lifecycle, packet dispatch and shared session helpers."""
from __future__ import annotations

import asyncio
import math
import os
import struct
import time

from src.common.config import Config
from src.common.logger import setup_logger
from src.crypto.world_crypt import WorldCrypt
from src.network.packet_header import ClientPacketHeader
from src.network.packet_header import ServerPacketHeader
from src.network.packet_log import get_packet_log
from src.network.packet_log import Direction
from src.protocol.opcodes import Opcodes
from src.world import creature
from src.world import geometry
from src.world import login_packets
from src.world import los
from src.world import movement
from src.world import object_update

from .constants import (
    MOVEMENT_OPCODES,
)

class SocketBase:
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, config: Config,
                 server=None):
        self.reader = reader
        self.writer = writer
        self.config = config
        self.server = server
        self.log = setup_logger("worldsocket", config.logging.log_dir, config.logging.level, config.logging.console)
        self.packet_log = get_packet_log()
        # Anchor for the ``uint32`` cast timestamps the spell packets carry; any
        # increasing millisecond clock works (TrinityCore uses the map tick).
        self._session_start = time.monotonic()

        self.authenticated = False
        self.should_close = False
        self.session_key = None
        self.crypt: WorldCrypt | None = None
        self.account_id = None
        self.account_name = None
        self.expansion = 2

        # Set while the client is inside the world (after CMSG_PLAYER_LOGIN).
        self.in_world = False
        self.player_guid = None
        self.character = None
        # True once the world-entry burst has been sent, so other sessions
        # never send create blocks to a client that is still loading.
        self.world_entry_complete = False
        # True once the destroy packet has been broadcast for this stay in the
        # world, so logout followed by a disconnect sends it exactly once.
        self._despawn_sent = False

        # Secure addons announced in CMSG_AUTH_SESSION; SMSG_ADDON_INFO has to
        # answer with one record per entry, in the same order.
        self.addon_names: list[str] = []

        # Movement state: last MovementInfo the client reported, the active
        # mover it selected and the throttling timestamp for DB writes.
        self.movement_info: movement.MovementInfo | None = None
        self.active_mover_guid = 0
        self._last_position_save = 0.0

        # Static creatures currently created on this client, keyed by the low
        # guid stored in ``creature_spawns``.  Used to send only the difference
        # when the player walks into a new part of the zone.
        self._visible_creatures: dict[int, int] = {}
        self._creature_scan_pos: tuple[float, float, float] | None = None
        # Game objects on the client: spawn guid -> full object guid, and the
        # (map, x, y, z) of the last visibility scan.
        self._visible_gameobjects: dict[int, int] = {}
        self._gameobject_scan_pos: tuple[int, float, float, float] | None = None
        self._gameobject_spawn_cache: dict[int, dict] = {}
        self._gameobject_dynamic: dict[int, int] = {}
        # Spawn rows for the visible creatures, kept so the idle-movement tick
        # does not hit SQLite for every creature every 250 ms.
        self._creature_spawn_cache: dict[int, object] = {}

        # Summoned pets/guardians are session-local creature rows.  They share
        # the spawn cache so movement/health code can find them by low guid.
        self._summoned_creatures: dict[int, dict] = {}
        self._summon_states: dict[int, dict] = {}
        self._summon_serial = 0

        # True between SMSG_NEW_WORLD (release spirit / teleport) and the
        # client's MSG_MOVE_WORLDPORT_ACK.  The world contents must not be
        # re-sent until that acknowledgement arrives.
        self._repop_pending = False

        self.server_challenge = os.urandom(4)
        self.dos_challenge = os.urandom(32)

    async def send_packet(self, opcode: int, data: bytes = b""):
        if self.should_close:
            return
        try:
            size = 2 + len(data)
            header = ServerPacketHeader(size, opcode).build()
            if self.crypt is not None:
                header = self.crypt.encrypt(header)
            packet = header + data

            if self.packet_log.CanLogPacket():
                self.packet_log.LogPacket(data, opcode, Direction.SERVER_TO_CLIENT)

            self.writer.write(packet)
            await self.writer.drain()
            self.log.debug(f"SMSG 0x{opcode:04X} sent ({len(data)} bytes)")
        except Exception as exc:
            self.log.debug(f"Send packet error: {exc}")

    def _build_values_update_block(self, guid: int, max_field: int, changed_fields: dict) -> bytes:
        """Build a single UPDATETYPE_VALUES block for SMSG_UPDATE_OBJECT."""
        import struct
        from src.world import update_fields as F
        blocks = (max_field + 31) // 32
        mask = bytearray(blocks * 4)
        for index in changed_fields:
            mask[(index // 32) * 4 + (index % 32) // 8] |= 1 << (index % 8)

        block = bytearray()
        block.append(object_update.UPDATETYPE_VALUES)
        block += object_update.pack_guid(guid)
        block.append(blocks)
        block += mask
        for index in sorted(changed_fields):
            block += struct.pack("<I", changed_fields[index] & 0xFFFFFFFF)
        return bytes(block)

    async def handle(self):
        peer = self.writer.get_extra_info("peername")
        self.log.info(f"World client connected: {peer}")
        try:
            await self.send_auth_challenge()
            # The combat/AI tick runs for the whole connection but does nothing
            # until a character is actually in the world.
            self.start_combat_loop()

            while not self.should_close:
                header_bytes = await asyncio.wait_for(
                    self.reader.readexactly(6), timeout=300
                )
                if self.crypt is not None:
                    header_bytes = self.crypt.decrypt(header_bytes)

                parsed = ClientPacketHeader.parse(header_bytes)
                if parsed is None:
                    break
                header, _ = parsed
                if not header.is_valid():
                    self.log.warning(f"Malformed client header: size={header.size} opcode=0x{header.opcode:X}")
                    break

                payload = b""
                if header.payload_size:
                    payload = await self.reader.readexactly(header.payload_size)

                if self.packet_log.CanLogPacket():
                    self.packet_log.LogPacket(payload, header.opcode, Direction.CLIENT_TO_SERVER)

                name = Opcodes(header.opcode).name if header.opcode in Opcodes._value2member_map_ else "UNKNOWN"
                self.log.debug(f"CMSG 0x{header.opcode:04X} {name} ({len(payload)} bytes)")

                await self.handle_packet(header.opcode, payload)

        except (asyncio.IncompleteReadError, asyncio.TimeoutError,
                ConnectionError) as exc:
            # A normal client disconnect on Windows often surfaces as
            # ConnectionAbortedError (WinError 10053), not IncompleteReadError.
            self.log.debug(f"World client disconnected: {exc}")
        except Exception as exc:
            self.log.exception(f"World socket error: {exc}")
        finally:
            self.stop_combat_loop()
            try:
                self.persist_position(force=True)
            except Exception:
                pass
            # Tell the other clients to remove this character before the
            # socket goes away; without it a crashed/timed-out client would
            # leave a frozen copy standing in the world.
            try:
                await self.dismiss_all_summons()
            except Exception:
                pass
            try:
                await self.despawn_for_peers()
            except Exception:
                pass
            try:
                self.writer.close()
                await self.writer.wait_closed()
            except Exception:
                pass
            self.log.info(f"World client disconnected: {peer}")

    async def handle_packet(self, opcode: int, data: bytes):
        try:
            if opcode == Opcodes.CMSG_AUTH_SESSION:
                await self.handle_auth_session(data)
            elif opcode == Opcodes.CMSG_PING:
                await self.handle_ping(data)
            elif opcode == Opcodes.CMSG_CHAR_ENUM:
                await self.handle_char_enum(data)
            elif opcode == Opcodes.CMSG_CHAR_CREATE:
                await self.handle_char_create(data)
            elif opcode == Opcodes.CMSG_CHAR_DELETE:
                await self.handle_char_delete(data)
            elif opcode == Opcodes.CMSG_REALM_SPLIT:
                await self.handle_realm_split(data)
            elif opcode == Opcodes.CMSG_PLAYER_LOGIN:
                await self.handle_player_login(data)
            elif opcode == Opcodes.CMSG_LOGOUT_REQUEST:
                await self.handle_logout_request(data)
            elif opcode == Opcodes.CMSG_LOGOUT_CANCEL:
                await self.send_packet(Opcodes.SMSG_LOGOUT_CANCEL_ACK, b"")
            elif opcode == Opcodes.CMSG_READY_FOR_ACCOUNT_DATA_TIMES:
                await self.send_packet(
                    Opcodes.SMSG_ACCOUNT_DATA_TIMES,
                    login_packets.build_account_data_times(),
                )
            elif opcode == Opcodes.CMSG_REQUEST_ACCOUNT_DATA:
                await self.handle_request_account_data(data)
            elif opcode == Opcodes.CMSG_UPDATE_ACCOUNT_DATA:
                await self.handle_update_account_data(data)
            elif opcode == Opcodes.CMSG_NAME_QUERY:
                await self.handle_name_query(data)
            elif opcode == Opcodes.CMSG_QUERY_TIME:
                await self.send_packet(
                    Opcodes.SMSG_QUERY_TIME_RESPONSE,
                    login_packets.build_query_time_response(),
                )
            elif opcode == Opcodes.CMSG_PLAYED_TIME:
                await self.send_packet(
                    Opcodes.SMSG_PLAYED_TIME,
                    login_packets.build_played_time_response(),
                )
            elif opcode == Opcodes.CMSG_ITEM_QUERY_SINGLE:
                await self.handle_item_query(data)
            elif opcode == Opcodes.CMSG_CREATURE_QUERY:
                await self.handle_creature_query(data)
            elif opcode == Opcodes.CMSG_GAMEOBJECT_QUERY:
                await self.handle_gameobject_query(data)
            elif opcode == Opcodes.CMSG_GAMEOBJ_USE:
                await self.handle_gameobject_use(data)
            elif opcode == Opcodes.CMSG_GAMEOBJ_REPORT_USE:
                pass                      # achievement criteria only
            elif opcode == Opcodes.CMSG_USE_ITEM:
                await self.handle_use_item(data)
            elif opcode == Opcodes.CMSG_PAGE_TEXT_QUERY:
                await self.handle_page_text_query(data)
            elif opcode == Opcodes.CMSG_GET_MAIL_LIST:
                await self.handle_get_mail_list(data)
            elif opcode == Opcodes.MSG_QUERY_NEXT_MAIL_TIME:
                await self.handle_query_next_mail_time(data)
            elif opcode == Opcodes.MSG_MOVE_TELEPORT_ACK:
                await self.handle_teleport_ack(data)
            elif opcode == Opcodes.CMSG_GOSSIP_HELLO:
                await self.handle_gossip_hello(data)
            elif opcode == Opcodes.CMSG_GOSSIP_SELECT_OPTION:
                await self.handle_gossip_select_option(data)
            elif opcode == Opcodes.CMSG_NPC_TEXT_QUERY:
                await self.handle_npc_text_query(data)
            elif opcode == Opcodes.CMSG_QUESTGIVER_STATUS_QUERY:
                await self.handle_questgiver_status_query(data)
            elif opcode == Opcodes.CMSG_QUESTGIVER_STATUS_MULTIPLE_QUERY:
                await self.handle_questgiver_status_multiple_query(data)
            elif opcode == Opcodes.CMSG_QUESTGIVER_HELLO:
                await self.handle_questgiver_hello(data)
            elif opcode == Opcodes.CMSG_QUESTGIVER_QUERY_QUEST:
                await self.handle_questgiver_query_quest(data)
            elif opcode == Opcodes.CMSG_QUESTGIVER_ACCEPT_QUEST:
                await self.handle_questgiver_accept_quest(data)
            elif opcode == Opcodes.CMSG_QUESTGIVER_COMPLETE_QUEST:
                await self.handle_questgiver_complete_quest(data)
            elif opcode == Opcodes.CMSG_QUESTGIVER_REQUEST_REWARD:
                await self.handle_questgiver_request_reward(data)
            elif opcode == Opcodes.CMSG_QUESTGIVER_CHOOSE_REWARD:
                await self.handle_questgiver_choose_reward(data)
            elif opcode == Opcodes.CMSG_QUESTGIVER_CANCEL:
                await self.handle_questgiver_cancel(data)
            elif opcode == Opcodes.CMSG_QUESTLOG_REMOVE_QUEST:
                await self.handle_questlog_remove_quest(data)
            elif opcode == Opcodes.CMSG_QUEST_QUERY:
                await self.handle_quest_query(data)
            elif opcode == Opcodes.CMSG_QUEST_POI_QUERY:
                await self.handle_quest_poi_query(data)
            elif opcode == Opcodes.CMSG_AREATRIGGER:
                await self.handle_area_trigger(data)
            elif opcode == Opcodes.CMSG_SWAP_INV_ITEM:
                await self.handle_swap_inv_item(data)
            elif opcode == Opcodes.CMSG_SWAP_ITEM:
                await self.handle_swap_item(data)
            elif opcode == Opcodes.CMSG_AUTOEQUIP_ITEM:
                await self.handle_autoequip_item(data)
            elif opcode == Opcodes.CMSG_DESTROYITEM:
                await self.handle_destroy_item(data)
            elif opcode == Opcodes.CMSG_GET_MIRRORIMAGE_DATA:
                await self.handle_get_mirrorimage_data(data)
            elif opcode == Opcodes.CMSG_CALENDAR_GET_NUM_PENDING:
                await self.send_packet(
                    Opcodes.SMSG_CALENDAR_SEND_NUM_PENDING,
                    login_packets.build_calendar_num_pending(),
                )
            elif opcode == Opcodes.CMSG_MESSAGECHAT:
                await self.handle_message_chat(data)
            elif opcode == Opcodes.CMSG_TEXT_EMOTE:
                await self.handle_text_emote(data)
            elif opcode == Opcodes.CMSG_EMOTE:
                await self.handle_emote(data)
            elif opcode == Opcodes.CMSG_STANDSTATECHANGE:
                await self.handle_stand_state_change(data)
            elif opcode == Opcodes.CMSG_SET_SHEATHED:
                await self.handle_set_sheathed(data)
            elif opcode == Opcodes.CMSG_SET_ACTIVE_MOVER:
                await self.handle_set_active_mover(data)
            elif opcode == Opcodes.CMSG_ZONEUPDATE:
                await self.handle_zone_update(data)
            elif opcode == Opcodes.CMSG_ATTACKSWING:
                await self.handle_attack_swing(data)
            elif opcode == Opcodes.CMSG_ATTACKSTOP:
                await self.handle_attack_stop(data)
            elif opcode == Opcodes.CMSG_CAST_SPELL:
                await self.handle_cast_spell(data)
            elif opcode == Opcodes.CMSG_CANCEL_CAST:
                await self.handle_cancel_cast(data)
            elif opcode == Opcodes.CMSG_CANCEL_CHANNELLING:
                await self.handle_cancel_channel(data)
            elif opcode == Opcodes.CMSG_CANCEL_AURA:
                await self.handle_cancel_aura(data)
            elif opcode == Opcodes.CMSG_CANCEL_TEMP_ENCHANTMENT:
                await self.handle_cancel_temp_enchantment(data)
            elif opcode == Opcodes.CMSG_SET_SELECTION:
                await self.handle_set_selection(data)
            elif opcode == Opcodes.CMSG_LOOT:
                await self.handle_loot(data)
            elif opcode == Opcodes.CMSG_GROUP_INVITE:
                await self.handle_group_invite(data)
            elif opcode == Opcodes.CMSG_GROUP_ACCEPT:
                await self.handle_group_accept(data)
            elif opcode == Opcodes.CMSG_GROUP_DECLINE:
                await self.handle_group_decline(data)
            elif opcode == Opcodes.CMSG_GROUP_UNINVITE:
                await self.handle_group_uninvite(data)
            elif opcode == Opcodes.CMSG_GROUP_UNINVITE_GUID:
                await self.handle_group_uninvite_guid(data)
            elif opcode == Opcodes.CMSG_GROUP_SET_LEADER:
                await self.handle_group_set_leader(data)
            elif opcode == Opcodes.CMSG_GROUP_DISBAND:
                await self.handle_group_disband(data)
            elif opcode == Opcodes.CMSG_GROUP_RAID_CONVERT:
                await self.handle_group_raid_convert(data)
            elif opcode == Opcodes.CMSG_GROUP_CHANGE_SUB_GROUP:
                await self.handle_group_change_subgroup(data)
            elif opcode == Opcodes.CMSG_GROUP_SWAP_SUB_GROUP:
                await self.handle_group_swap_subgroup(data)
            elif opcode == Opcodes.CMSG_REQUEST_PARTY_MEMBER_STATS:
                await self.handle_request_party_member_stats(data)
            elif opcode == Opcodes.CMSG_LOOT_RELEASE:
                await self.handle_loot_release(data)
            elif opcode == Opcodes.CMSG_LOOT_MONEY:
                await self.handle_loot_money(data)
            elif opcode == Opcodes.CMSG_AUTOSTORE_LOOT_ITEM:
                await self.handle_autostore_loot_item(data)
            elif opcode == Opcodes.CMSG_LIST_INVENTORY:
                await self.handle_list_inventory(data)
            elif opcode == Opcodes.CMSG_BUY_ITEM:
                await self.handle_buy_item(data)
            elif opcode == Opcodes.CMSG_BUY_ITEM_IN_SLOT:
                await self.handle_buy_item(data, in_slot=True)
            elif opcode == Opcodes.CMSG_SELL_ITEM:
                await self.handle_sell_item(data)
            elif opcode == Opcodes.CMSG_REPOP_REQUEST:
                await self.handle_repop_request(data)
            elif opcode == Opcodes.MSG_MOVE_WORLDPORT_ACK:
                await self.handle_worldport_ack(data)
            elif opcode in MOVEMENT_OPCODES:
                await self.handle_movement(opcode, data)
            elif opcode in (Opcodes.CMSG_KEEP_ALIVE, Opcodes.CMSG_TIME_SYNC_RESP):
                pass
            else:
                self.log.debug(f"Unhandled opcode 0x{opcode:04X}")
            # Rage gain and regeneration are computed in synchronous helpers;
            # push whatever they queued once the packet is done.
            await self._flush_player_fields()
        except Exception as exc:
            self.log.exception(f"Packet handler error for opcode 0x{opcode:04X}: {exc}")

    def _get_combat_state(self) -> dict:
        """Return the in-memory combat state for this session."""
        if not hasattr(self, "_combat_state"):
            self._combat_state = {
                "in_combat": False,
                "target_guid": 0,
                "auto_attacking": False,
                "ranged_attacking": False,
                "ranged_spell_id": 75,
                "last_ranged_swing": 0.0,
                "ranged_interval": 2.0,
                "last_swing": 0.0,
                "swing_interval": 2.0,
                "pending_cast": None,
                "channel": None,
                "school_lockout": {},
                "cooldowns": {},
                # low_guid -> {last_swing, chase_until}
                "aggro": {},
                # Misdirection/Tricks-style threat redirect for this player.
                "threat_redirect": None,
                # active damage/heal over time effects
                "dots": [],
                # persistent ground AoE effects (Blizzard, Consecration, ...)
                "area_effects": [],
                # modifier auras (slow/root/stun/stat/...), player + creatures
                "auras": [],
                "creature_auras": {},
                "aura_state": {},
                "loot_target": 0,
                "loot_items": [],
                "loot_taken": [],
                "loot_money": 0,
                "selected_guid": 0,
                "dead": False,
                "last_respawn_scan": 0.0,
                "gcd_until": 0.0,
                # Timestamp until which natural mana regeneration is blocked.
                # ``Spell::SendCastResult`` sets it whenever a spell spends
                # power, mirroring CMaNGOS' ``IsUnderLastManaUseEffect``.
                "last_power_use": 0.0,
                # Rogue combo points are per-target state (``Unit::
                # m_comboTargetGuid``/``m_comboPoints``).  They survive
                # stopping auto-attack, but are lost when the target dies or
                # when the rogue opens on a different target.
                "combo_target": 0,
                "combo_points": 0,
                # slot -> temporary weapon enchant/poison and per-item proc state
                "weapon_enchants": {},
                "weapon_proc_cooldowns": {},
                "weapon_proc_charges": {},
                # Death knight runes: one entry per slot with the base type,
                # the current type (death runes convert back when spent) and
                # the remaining cooldown in milliseconds.
                "runes": [],
                "last_rune_regen": 0.0,
                # Rune prediction for the SMSG_SPELL_GO that follows a spend.
                "rune_prediction": None,
                # Idle movement state per visible creature (wander / waypoint).
                "idle": {},
                # low_guid -> monotonic time until which idle movement must not
                # override the "walk home" spline after evading.
                "idle_settle": {},
            }
        return self._combat_state

    @staticmethod
    def _read_cstring(data: bytes, pos: int):
        end = data.find(b"\x00", pos)
        if end < 0:
            raise ValueError("unterminated string")
        return data[pos:end].decode("utf-8", errors="replace"), end + 1

    def _server_tick_ms(self) -> int:
        """Monotonic server tick, the ``uint32`` timestamp of ``SpellCastData``.

        TrinityCore writes ``Map::GetCurrentMSTime()`` here; any increasing value
        works because the client only uses it to align the cast animation.
        """
        return int((time.monotonic() - self._session_start) * 1000.0) & 0xFFFFFFFF

    def _is_player_guid(self, guid: int) -> bool:
        """True for our own character (the only player this session can see)."""
        return int(guid or 0) == int(self.player_guid or 0)

    @staticmethod
    def _read_packed_guid(data: bytes, pos: int):
        """Read one ``ObjectGuid::ReadAsPacked`` value; returns ``(guid, new_pos)``."""
        if pos >= len(data):
            return 0, pos
        mask = data[pos]
        pos += 1
        guid = 0
        for index in range(8):
            if not (mask >> index) & 1:
                continue
            if pos >= len(data):
                return guid, pos
            guid |= data[pos] << (index * 8)
            pos += 1
        return guid, pos

    def _next_move_id(self) -> int:
        """Return a monotonically increasing spline id.

        TrinityCore assigns every ``SMSG_MONSTER_MOVE`` a fresh id from a global
        counter.  Reusing 0 makes some clients ignore a second spline for the
        same unit (the facing spline never gets applied).
        """
        combat = self._get_combat_state()
        combat["move_id"] = (int(combat.get("move_id", 0)) + 1) & 0xFFFFFFFF
        return combat["move_id"]

    def _player_position(self) -> tuple[float, float, float]:
        if not self.character:
            return (0.0, 0.0, 0.0)
        return (float(self.character["x"]), float(self.character["y"]),
                float(self.character["z"]))

    @staticmethod
    def _distance(a: tuple[float, float, float],
                  b: tuple[float, float, float]) -> float:
        return ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2) ** 0.5

    @staticmethod
    def _normalised_delta(angle: float, facing: float) -> float:
        return (angle - facing + math.pi) % (2.0 * math.pi) - math.pi

    def _in_front(self, observer: tuple[float, float, float], facing: float,
                  target: tuple[float, float, float],
                  arc: float = math.pi) -> bool:
        """True when ``target`` lies inside a frontal arc.

        ``arc`` is the full arc width in radians; the reference melee/facing
        checks use a 180-degree frontal arc.
        """
        dx = target[0] - observer[0]
        dy = target[1] - observer[1]
        if dx * dx + dy * dy < 0.0001:
            return True
        delta = SocketBase._normalised_delta(math.atan2(dy, dx), facing)
        return abs(delta) <= arc / 2.0 + 1e-6

    def _unit_facing(self, guid: int) -> float:
        if self._is_player_guid(guid) and self.character:
            return float(self.character.get("o", 0.0) or 0.0)
        low = creature.creature_low_guid(int(guid or 0))
        state = self._get_combat_state().get("aggro", {}).get(low)
        if state is not None and state.get("facing") is not None:
            return float(state["facing"])
        spawn = self._get_creature_spawn(low)
        if spawn is not None and "o" in spawn.keys():
            return float(spawn["o"] or 0.0)
        return 0.0

    def _unit_eye_position(self, guid: int) -> tuple[float, float, float]:
        if self._is_player_guid(guid):
            pos = self._player_position()
        else:
            low = creature.creature_low_guid(int(guid or 0))
            pos = self._creature_position(low)
        return (pos[0], pos[1], pos[2] + 1.6)

    def _get_geometry(self):
        """Shared extracted terrain/VMap/MMap data, or ``None`` when absent."""
        if not hasattr(self, "_world_geometry"):
            self._world_geometry = geometry.get_geometry(
                self.config, getattr(self.config.database, "world_path", None))
        return self._world_geometry

    def _current_map_id(self) -> int:
        return int((getattr(self, "character", None) or {}).get("map", 0) or 0)

    def _has_line_of_sight(self, start, end, map_id: int | None = None) -> bool:
        if not getattr(self, "character", None):
            return True
        if map_id is None:
            map_id = (self._current_map_id()
                      if hasattr(self, "_current_map_id")
                      else int(self.character.get("map", 0) or 0))
        start = (float(start[0]), float(start[1]), float(start[2]))
        end = (float(end[0]), float(end[1]), float(end[2]))
        world = (self._get_geometry()
                 if hasattr(self, "_get_geometry") else None)
        if world is not None:
            return world.has_line_of_sight(int(map_id), start, end)
        return los.has_los(
            self.config.database.world_path, int(map_id), start, end)

    def _ground_height(self, x: float, y: float,
                       map_id: int | None = None) -> float | None:
        world = self._get_geometry()
        if world is None:
            return None
        return world.ground_height(
            self._current_map_id() if map_id is None else int(map_id),
            float(x), float(y))

    def _find_world_path(self, start, end, max_tiles: int = 2):
        """Navmesh waypoints for ``start`` -> ``end`` (``None`` when unavailable)."""
        world = self._get_geometry()
        if world is None:
            return None
        return world.find_path(self._current_map_id(), start, end,
                               max_tiles=max_tiles)

    def _navmesh_point(self, point, max_distance: float = 8.0):
        world = self._get_geometry()
        if world is None:
            return None
        return world.point_on_mesh(self._current_map_id(), point)

    def _random_world_point(self, center, radius: float):
        world = self._get_geometry()
        if world is None:
            return None
        return world.random_point(self._current_map_id(), center, radius)

    @staticmethod
    def _path_prefix(start, path, travel: float, stop_distance: float = 0.0):
        """Return the path points reachable within ``travel`` yards.

        The final point is interpolated so a caller can cap a spline at a
        known duration.  ``stop_distance`` leaves room for melee reach.
        """
        if not path:
            return []
        remaining = max(0.0, float(travel))
        if stop_distance:
            # Do not spend the last yards that are reserved for melee.
            total = 0.0
            for index in range(1, len(path)):
                total += ((path[index][0] - path[index - 1][0]) ** 2
                          + (path[index][1] - path[index - 1][1]) ** 2
                          + (path[index][2] - path[index - 1][2]) ** 2) ** 0.5
            remaining = max(0.0, min(remaining, total - stop_distance))
        points = []
        current = (float(start[0]), float(start[1]), float(start[2]))
        for point in path[1:]:
            dx = point[0] - current[0]
            dy = point[1] - current[1]
            dz = point[2] - current[2]
            segment = (dx * dx + dy * dy + dz * dz) ** 0.5
            if segment <= 1e-6:
                continue
            if segment >= remaining:
                if remaining > 0.05:
                    scale = remaining / segment
                    points.append((current[0] + dx * scale,
                                   current[1] + dy * scale,
                                   current[2] + dz * scale))
                break
            points.append((float(point[0]), float(point[1]),
                           float(point[2])))
            remaining -= segment
            current = points[-1]
        return points

    def _target_in_front(self, target_guid: int, arc: float = math.pi) -> bool:
        if not target_guid or self._is_player_guid(target_guid):
            return True
        observer = self._player_position()
        if self._is_peer_player_guid(target_guid):
            peer = self._peer_session_for_guid(target_guid)
            if peer is None or not peer.character:
                return True
            target = (float(peer.character.get("x", 0.0)),
                      float(peer.character.get("y", 0.0)),
                      float(peer.character.get("z", 0.0)))
        else:
            low = creature.creature_low_guid(int(target_guid))
            spawn = self._get_creature_spawn(low)
            if spawn is None:
                return True
            target = self._creature_position(low)
            # Standing inside a large target's collision volume should never
            # fail, but ordinary melee range must still require facing.
            radius = 1.0
            if "bounding_radius" in spawn.keys():
                radius += max(0.0, float(spawn["bounding_radius"] or 0.0))
            if self._distance(observer, target) <= min(2.5, radius):
                return True
        return self._in_front(
            observer, float(self.character.get("o", 0.0) or 0.0),
            target, arc)

    def _unit_is_alive(self, guid: int) -> bool:
        """True while ``guid`` can hold combo points.

        Used by :meth:`_add_combo_points` because Sinister Strike's damage
        effect runs before its combo-point effect in our immediate-apply loop.
        The core adds the points during the effect loop and lets
        ``DealDamage`` -> ``ClearComboPointHolders`` wipe them when the blow is
        fatal; checking liveness here reproduces that net result.
        """
        if not guid:
            return False
        if self._is_player_guid(guid):
            return not bool(self._get_combat_state()["dead"])
        spawn = self._get_creature_spawn(creature.creature_low_guid(guid))
        if spawn is None:
            # Unknown guid (a peer session or an in-process test double): it
            # cannot have died in this session, so let the points stand.
            return True
        return self._creature_health(guid) > 0
