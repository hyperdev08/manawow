"""Character list/create/delete plus query and gossip handlers."""
from __future__ import annotations

import sqlite3
import struct

from src.protocol.opcodes import Opcodes
from src.world import character_db
from src.world import creature
from src.world import trainer
from src.world import vendor
from src.world import login_packets

from .constants import (
    CHAR_CREATE_FAILED,
    CHAR_CREATE_NAME_IN_USE,
    CHAR_CREATE_SUCCESS,
    CHAR_DELETE_FAILED,
    CHAR_DELETE_SUCCESS,
)

class CharactersMixin:
    async def handle_char_enum(self, data: bytes):
        self.log.info("Handling CMSG_CHAR_ENUM")
        characters = character_db.list_characters(self.config.database.world_path, self.account_id)
        # Fetch equipment for each character so the enum shows equipped items
        equipment = {}
        for char in characters:
            guid = int(char["guid"])
            equipment[guid] = character_db.get_equipment_for_char_enum(
                self.config.database.world_path, guid
            )
        payload = character_db.build_char_enum_payload(characters, equipment)
        await self.send_packet(Opcodes.SMSG_CHAR_ENUM, payload)
        self.log.info(f"Sent SMSG_CHAR_ENUM with {len(characters)} character(s)")

    async def handle_char_create(self, data: bytes):
        result = CHAR_CREATE_FAILED
        try:
            name, pos = self._read_cstring(data, 0)
            race, char_class, gender, skin, face, hair_style, hair_color, facial_style, _outfit_id = data[pos:pos + 9]
            normalized = character_db.normalize_name(name)

            if (normalized is None
                    or not character_db.is_valid_race_class(race, char_class)
                    or gender not in (0, 1)):
                result = CHAR_CREATE_FAILED
            else:
                try:
                    guid = character_db.create_character(
                        self.config.database.world_path,
                        self.account_id,
                        normalized,
                        race,
                        char_class,
                        gender,
                        skin,
                        face,
                        hair_style,
                        hair_color,
                        facial_style,
                    )
                    self.log.info(f"Created character '{normalized}' (guid {guid}) for account {self.account_name}")
                    # Grant starter items to the new character
                    character_db.grant_starter_items(self.config.database.world_path, guid, race, char_class)
                    result = CHAR_CREATE_SUCCESS
                except sqlite3.IntegrityError:
                    result = CHAR_CREATE_NAME_IN_USE
        except Exception as exc:
            self.log.warning(f"CHAR_CREATE parse error: {exc}")

        await self.send_packet(Opcodes.SMSG_CHAR_CREATE, bytes([result]))
        self.log.info(f"Sent SMSG_CHAR_CREATE result {result}")

    async def handle_char_delete(self, data: bytes):
        result = CHAR_DELETE_FAILED
        if len(data) < 8:
            self.log.warning(f"CMSG_CHAR_DELETE truncated ({len(data)} bytes)")
        else:
            guid = struct.unpack_from("<Q", data, 0)[0]
            if guid == 0:
                self.log.warning("CMSG_CHAR_DELETE with guid 0")
            elif character_db.delete_character(self.config.database.world_path, self.account_id, guid):
                result = CHAR_DELETE_SUCCESS
                self.log.info(f"Deleted character guid {guid} for account {self.account_name}")
            else:
                # Not owned by this account, or already gone.
                self.log.warning(
                    f"Refused to delete character guid {guid} for account {self.account_name} "
                    "(not found or owned by another account)"
                )
        await self.send_packet(Opcodes.SMSG_CHAR_DELETE, bytes([result]))
        self.log.info(f"Sent SMSG_CHAR_DELETE result {result}")

    async def handle_name_query(self, data: bytes):
        guid = struct.unpack_from("<Q", data, 0)[0] if len(data) >= 8 else 0
        character = self.character if guid and guid == self.player_guid else None
        if character is None and guid:
            # Name queries are not account-scoped: the client asks about every
            # player it can see, including characters owned by other accounts.
            character = character_db.get_character_by_guid(
                self.config.database.world_path, guid
            )
        if character is not None:
            await self.send_packet(
                Opcodes.SMSG_NAME_QUERY_RESPONSE,
                login_packets.build_name_query_response(character),
            )
            return

        # Creature GUIDs carry the CREATURE_HIGH_GUID prefix.  The 3.3.5 client
        # sends the same CMSG_NAME_QUERY for creatures; we answer with the
        # template's name so the nameplate shows without clicking.
        if guid and (guid >> 48) == 0xF130:  # CREATURE_HIGH_GUID prefix
            low_guid = creature.creature_low_guid(guid)
            spawn = creature.get_creature_spawn(
                self.config.database.world_path, low_guid
            )
            if spawn is not None:
                template = creature.get_creature_template(
                    self.config.database.world_path, int(spawn["entry"])
                )
                if template is not None:
                    # ``get_creature_template`` returns a sqlite3.Row: it can be
                    # read by index but not assigned to and has no ``.get``.
                    # The response builder needs both, so copy it to a dict.
                    name_template = dict(template)
                    name_template["low_guid"] = low_guid
                    await self.send_packet(
                        Opcodes.SMSG_NAME_QUERY_RESPONSE,
                        login_packets.build_creature_name_query_response(
                            name_template
                        ),
                    )
                    return

        await self.send_packet(
            Opcodes.SMSG_NAME_QUERY_RESPONSE,
            login_packets.build_name_query_failure(guid),
        )

    async def handle_item_query(self, data: bytes):
        item_id = struct.unpack_from("<I", data, 0)[0] if len(data) >= 4 else 0
        template = character_db.get_item_template(self.config.database.world_path, item_id)
        if template is None:
            self.log.debug(f"Item query for unknown entry {item_id}")
            await self.send_packet(
                Opcodes.SMSG_ITEM_QUERY_SINGLE_RESPONSE,
                login_packets.build_item_query_not_found(item_id),
            )
        else:
            self.log.debug(f"Item query for {item_id} -> {template['name1']}")
            await self.send_packet(
                Opcodes.SMSG_ITEM_QUERY_SINGLE_RESPONSE,
                login_packets.build_item_query_response(template),
            )

    async def handle_creature_query(self, data: bytes):
        """``CMSG_CREATURE_QUERY``: the client asks for a creature's stats.

        TrinityCore's ``QueryCreature::Read`` reads ``uint32 creatureId`` then
        ``uint64 guid``.  The entry is the authoritative lookup key; if it is
        unknown we fall back to the spawn guid so a client that sends the fields
        in the other order still gets an answer.
        """
        if len(data) < 4:
            return
        entry = struct.unpack_from("<I", data, 0)[0]
        guid = struct.unpack_from("<Q", data, 4)[0] if len(data) >= 12 else 0

        template = creature.get_creature_template(self.config.database.world_path, entry)
        if template is None and guid:
            spawn = creature.get_creature_spawn(
                self.config.database.world_path, creature.creature_low_guid(guid)
            )
            if spawn is not None:
                entry = int(spawn["entry"])
                template = creature.get_creature_template(
                    self.config.database.world_path, entry
                )
        if template is None and len(data) >= 12:
            # Defensive: entry at offset 8 (guid-first layout).
            alternate = struct.unpack_from("<I", data, 8)[0]
            template = creature.get_creature_template(
                self.config.database.world_path, alternate
            )
            if template is not None:
                entry = alternate

        if template is None:
            self.log.debug(f"Creature query for unknown entry {entry}")
            await self.send_packet(
                Opcodes.SMSG_CREATURE_QUERY_RESPONSE,
                login_packets.build_creature_query_not_found(entry),
            )
            return

        await self.send_packet(
            Opcodes.SMSG_CREATURE_QUERY_RESPONSE,
            login_packets.build_creature_query_response(template),
        )

    async def handle_npc_text_query(self, data: bytes):
        """``CMSG_NPC_TEXT_QUERY``: send the text the gossip window requested."""
        if len(data) < 4:
            return
        text_id = struct.unpack_from("<I", data, 0)[0]
        row = creature.get_npc_text(self.config.database.world_path, text_id)
        if row is None:
            self.log.debug(f"NPC text query for unknown id {text_id}")
            await self.send_packet(
                Opcodes.SMSG_NPC_TEXT_UPDATE,
                login_packets.build_npc_text_update(text_id, None),
            )
            return
        await self.send_packet(
            Opcodes.SMSG_NPC_TEXT_UPDATE,
            login_packets.build_npc_text_update(text_id, row),
        )

    async def _send_gossip_menu(self, guid: int, menu_id: int, quests=(),
                                vendor_option: bool = False,
                                spawn=None) -> bool:
        menu = creature.get_gossip_menu(self.config.database.world_path, menu_id)
        if menu is None:
            return False
        options = list(creature.get_gossip_options(
            self.config.database.world_path, menu_id
        ))
        if spawn is not None:
            # CMaNGOS hides the trainer option for a player who cannot use the
            # trainer (wrong class, wrong mount race, missing specialization).
            options = [
                option for option in options
                if int(option["option_type"]) != trainer.GOSSIP_OPTION_TRAINER
                or trainer.can_offer_trainer(
                    self.config.database.world_path, spawn, self.character,
                    getattr(self, "known_spell_ids", ()))
            ]
        if vendor_option and not any(
            int(o["option_type"]) == vendor.GOSSIP_OPTION_VENDOR for o in options
        ):
            options.append(vendor.vendor_gossip_option())
        await self.send_packet(
            Opcodes.SMSG_GOSSIP_MESSAGE,
            login_packets.build_gossip_message(
                guid, menu_id, int(menu["text_id"]), options, quests
            ),
        )
        return True

    async def handle_gossip_hello(self, data: bytes):
        """``CMSG_GOSSIP_HELLO``: open a creature's gossip menu."""
        if not self.in_world or len(data) < 8:
            return
        guid = struct.unpack_from("<Q", data, 0)[0]
        spawn = creature.get_creature_spawn(
            self.config.database.world_path, creature.creature_low_guid(guid)
        )
        if spawn is None:
            self.log.debug(f"Gossip hello for unknown creature guid {guid}")
            return
        if not (int(spawn["npc_flags"]) & 0x1):
            self.log.debug(f"Creature {guid} has no gossip flag")
            return

        is_vendor = bool(int(spawn["npc_flags"]) & vendor.NPC_FLAG_VENDOR)
        menu_id = int(spawn["gossip_menu_id"])
        quests = (self._gossip_quest_items(spawn)
                  if hasattr(self, "_gossip_quest_items") else [])
        has_options = is_vendor or bool(menu_id and creature.get_gossip_options(
            self.config.database.world_path, menu_id))
        if quests and not has_options:
            # ``Player::SendPreparedGossip``: a menu without options opens the
            # quest window (or the single quest) instead of the gossip window.
            if await self._send_quest_menu(guid, spawn):
                return
        if menu_id and await self._send_gossip_menu(
                guid, menu_id, quests, vendor_option=is_vendor, spawn=spawn):
            self.log.info(
                f"Opened gossip menu {menu_id} for creature "
                f"'{spawn['name']}' ({guid})"
            )
            return

        if is_vendor and not quests:
            # Nothing to say: go straight to the goods, like the real server.
            await self._send_vendor_list(guid, spawn)
            return
        if is_vendor:
            # A vendor that also offers quests but has no gossip menu: show
            # the quests next to a "browse your goods" option.
            await self.send_packet(
                Opcodes.SMSG_GOSSIP_MESSAGE,
                login_packets.build_gossip_message(
                    guid, menu_id, 0, [vendor.vendor_gossip_option()], quests),
            )
            return

        # A gossip-flagged NPC without a menu still gets an empty window.
        await self.send_packet(
            Opcodes.SMSG_GOSSIP_MESSAGE,
            login_packets.build_gossip_message(guid, menu_id, 0, (), quests),
        )

    async def handle_gossip_select_option(self, data: bytes):
        """``CMSG_GOSSIP_SELECT_OPTION``: switch menu or show an NPC text.

        Wire layout (client ``CGGossipInfo::SelectGossipOption``):
        uint64 guid, uint32 gossip menu id, uint32 option index, then an
        optional cstring when the option has ``coded & 1``.
        """
        if not self.in_world or len(data) < 16:
            return
        guid, menu_id, option_index = struct.unpack_from("<QII", data, 0)
        if option_index == vendor.VENDOR_GOSSIP_OPTION_INDEX:
            await self._open_vendor_from_gossip(guid)
            return
        option = creature.get_gossip_option(
            self.config.database.world_path, menu_id, option_index
        )
        if option is None:
            self.log.debug(
                f"Gossip option {option_index} missing from menu {menu_id}"
            )
            await self.send_packet(Opcodes.SMSG_GOSSIP_COMPLETE, b"")
            return

        if int(option["option_type"]) == trainer.GOSSIP_OPTION_TRAINER:
            # CMaNGOS sends only SMSG_TRAINER_LIST here; the trainer window
            # replaces the gossip window on the client.  Sending
            # SMSG_GOSSIP_COMPLETE first makes the 3.3.5 client drop the
            # interaction and ignore the following trainer list.
            spawn = self._trainer_spawn(guid)
            if spawn is not None and self._can_use_trainer(spawn):
                await self._send_trainer_list(guid, spawn)
            else:
                await self.send_packet(Opcodes.SMSG_GOSSIP_COMPLETE, b"")
            return
        if int(option["option_type"]) == vendor.GOSSIP_OPTION_VENDOR:
            await self._open_vendor_from_gossip(guid)
            return
        if int(option["option_type"]) == 6:      # GOSSIP_OPTION_SPIRITHEALER
            await self.send_packet(Opcodes.SMSG_GOSSIP_COMPLETE, b"")
            if hasattr(self, "handle_spirit_healer_activate"):
                await self.handle_spirit_healer_activate(
                    struct.pack("<Q", int(guid)))
            return

        action_menu_id = int(option["action_menu_id"])
        action_text_id = int(option["action_text_id"])
        submenu_spawn = (self._trainer_spawn(guid)
                         if hasattr(self, "_trainer_spawn") else None)
        if action_menu_id and await self._send_gossip_menu(
                guid, action_menu_id, spawn=submenu_spawn):
            self.log.info(
                f"Gossip menu {menu_id} option {option_index} -> menu "
                f"{action_menu_id}"
            )
            return
        if action_text_id:
            await self.send_packet(
                Opcodes.SMSG_GOSSIP_MESSAGE,
                login_packets.build_gossip_message(
                    guid, menu_id, action_text_id, ()
                ),
            )
            self.log.info(
                f"Gossip menu {menu_id} option {option_index} -> text "
                f"{action_text_id}"
            )
            return

        # An option with no action closes the gossip window.
        await self.send_packet(Opcodes.SMSG_GOSSIP_COMPLETE, b"")

    async def _open_vendor_from_gossip(self, guid: int) -> None:
        """A "browse your goods" gossip option: close the menu, show the goods."""
        spawn = self._vendor_spawn(guid)
        await self.send_packet(Opcodes.SMSG_GOSSIP_COMPLETE, b"")
        if spawn is not None:
            await self._send_vendor_list(guid, spawn)
