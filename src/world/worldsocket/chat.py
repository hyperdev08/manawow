"""Chat messages, text emotes and stand/sheath state."""
from __future__ import annotations

import struct

from src.common.db_io import run_db_write
from src.protocol.opcodes import Opcodes
from src.world import character_db
from src.world import login_packets
from src.world import object_update
from src.world import update_fields as F

from .constants import (
    CHAT_EMOTE_TO_TEXT_ID,
    STATE_EMOTE_IDS,
    TEXT_EMOTE_TO_EMOTE,
)

class ChatMixin:
    async def handle_message_chat(self, data: bytes):
        """Handle ``CMSG_MESSAGECHAT``: player chat message (say/yell/whisper/emote).

        Wire format (ChatFrame.cpp:897-916, ``Script_SendChatMessage``):
          uint32 type, uint32 language,
          [target_name] if whisper, then cstring message.

        We relay SAY/YELL/EMOTE to peers on the same map.  WHISPER goes to the
        target character (looked up by name) and the sender receives a
        WHISPER_INFORM echo.  AFK/DND are client-local state changes (type 23/24)
        that the client sends to itself; we just accept them silently.
        """
        if not self.in_world or not self.character:
            return

        pos = 0
        if len(data) < 8:
            return
        msg_type = struct.unpack_from("<I", data, pos)[0]
        pos += 4
        language = struct.unpack_from("<I", data, pos)[0]
        pos += 4

        self.log.debug(
            f"CMSG_MESSAGECHAT: type={msg_type} language={language} "
            f"data_len={len(data)}"
        )

        # Read target name if this is a whisper
        target_name = None
        if msg_type in (login_packets.CHAT_MSG_WHISPER, login_packets.CHAT_MSG_WHISPER_INFORM):
            if pos >= len(data):
                return
            target_name, pos = self._read_cstring(data, pos)

        # Read message text
        if pos >= len(data):
            return
        message, _ = self._read_cstring(data, pos)

        self.log.debug(f"Chat message: '{message}' (type={msg_type})")

        sender_guid = int(self.player_guid)
        sender_name = str(self.character["name"])

        if msg_type == login_packets.CHAT_MSG_WHISPER:
            # Find the target character by name
            target_char = character_db.find_character_by_name(
                self.config.database.world_path, target_name
            )
            if target_char is None:
                # Target not found: send "player not found" system message
                notfound = (
                    f"No player named '{target_name}' is currently playing."
                )
                payload = login_packets.build_message_chat_payload(
                    login_packets.CHAT_MSG_SYSTEM, 0, 0, "", sender_guid,
                    sender_name, notfound
                )
                await self.send_packet(Opcodes.SMSG_MESSAGECHAT, payload)
                return

            target_guid = int(target_char["guid"])
            target_name = str(target_char["name"])

            # Send to target (if online)
            target_session = None
            for session in getattr(self.server, "sessions", ()):
                if getattr(session, "in_world", False) and \
                   getattr(session, "player_guid", None) == target_guid:
                    target_session = session
                    break

            if target_session:
                # Deliver to target
                payload = login_packets.build_message_chat_payload(
                    login_packets.CHAT_MSG_WHISPER, language, sender_guid,
                    sender_name, target_guid, target_name, message
                )
                await target_session.send_packet(Opcodes.SMSG_MESSAGECHAT, payload)

                # Echo back to sender as WHISPER_INFORM.  The client resolves the
                # other side of a whisper from the *sender* guid
                # (ChatFrame.cpp:1618 -> RecordWhisperDest(senderGuid)), so the
                # target is the sender field and the speaker is the target
                # field, exactly like TrinityCore's Player::Whisper.
                payload = login_packets.build_message_chat_payload(
                    login_packets.CHAT_MSG_WHISPER_INFORM, language, target_guid,
                    target_name, sender_guid, sender_name, message
                )
                await self.send_packet(Opcodes.SMSG_MESSAGECHAT, payload)
            else:
                # Target offline: system message
                notfound = (
                    f"Player '{target_name}' not found."
                )
                payload = login_packets.build_message_chat_payload(
                    login_packets.CHAT_MSG_SYSTEM, 0, 0, "", sender_guid,
                    sender_name, notfound
                )
                await self.send_packet(Opcodes.SMSG_MESSAGECHAT, payload)

        elif msg_type in (login_packets.CHAT_MSG_PARTY,
                          login_packets.CHAT_MSG_RAID):
            manager = (self._group_manager()
                       if hasattr(self, "_group_manager") else None)
            if manager is None or manager.group_for(
                    int(self.player_guid)) is None:
                payload = login_packets.build_message_chat_payload(
                    login_packets.CHAT_MSG_SYSTEM, 0, 0, "",
                    sender_guid, sender_name, "You are not in a party.")
                await self.send_packet(Opcodes.SMSG_MESSAGECHAT, payload)
                return
            payload = login_packets.build_message_chat_payload(
                msg_type, language, sender_guid, sender_name, 0, "", message)
            for peer in manager.group_member_sessions(int(self.player_guid)):
                await peer.send_packet(Opcodes.SMSG_MESSAGECHAT, payload)

        elif msg_type in (login_packets.CHAT_MSG_SAY, login_packets.CHAT_MSG_YELL,
                          login_packets.CHAT_MSG_EMOTE):
            # Relay to all peers on the same map
            payload = login_packets.build_message_chat_payload(
                msg_type, language, sender_guid, sender_name, 0, "", message
            )
            await self.send_packet_to_peers(Opcodes.SMSG_MESSAGECHAT, payload)
            # Also send back to sender so their own message appears in chat log
            # (Client may discard it to avoid duplicate with local "You say:" echo)
            await self.send_packet(Opcodes.SMSG_MESSAGECHAT, payload)

            # For CHAT_MSG_EMOTE, also send the animation packet so peers see the emote
            if msg_type == login_packets.CHAT_MSG_EMOTE:
                await self._send_emote_animation(sender_guid, message)

        elif msg_type in (login_packets.CHAT_MSG_AFK, login_packets.CHAT_MSG_DND):
            # Client sets its own AFK/DND flag; no relay needed
            self.log.debug(f"Client set AFK/DND: type={msg_type}")
        else:
            self.log.debug(f"Unhandled chat type {msg_type}")

    async def handle_text_emote(self, data: bytes):
        """Handle ``CMSG_TEXT_EMOTE``: text emote (/wave, /dance, etc.).

        Wire format: uint32 text_emote_id, uint32 unk, uint64 target_guid.
        The client only sends the target guid for emotes that reference another
        player (e.g. /wave <target>).  We broadcast the emote to all peers so
        they see the animation.
        """
        if not self.in_world or not self.character:
            return

        if len(data) < 16:
            return
        pos = 0
        text_emote_id = struct.unpack_from("<I", data, pos)[0]
        pos += 4
        _unk = struct.unpack_from("<I", data, pos)[0]
        pos += 4
        target_guid = struct.unpack_from("<Q", data, pos)[0] if len(data) >= pos + 8 else 0

        sender_guid = int(self.player_guid)
        sender_name = str(self.character["name"])

        self.log.info(f"TEXT_EMOTE: text_emote_id={text_emote_id} sender={sender_name}({sender_guid}) target_guid={target_guid}")

        # Broadcast to peers
        payload = login_packets.build_text_emote_payload(text_emote_id, sender_guid, sender_name)
        await self.send_packet_to_peers(Opcodes.SMSG_TEXT_EMOTE, payload)
        # And to sender
        await self.send_packet(Opcodes.SMSG_TEXT_EMOTE, payload)

        # Map the text emote ID (EmotesText.dbc) to the EmotesRec ID (Emotes.dbc)
        # using the client's own DBC mapping.
        emote_id = TEXT_EMOTE_TO_EMOTE.get(text_emote_id, 0)
        self.log.info(f"TEXT_EMOTE: mapped to emote_id={emote_id}")
        if emote_id:
            # State emotes (dance, sit, sleep, kneel, etc.) use the NPC emote state
            # system via UNIT_NPC_EMOTESTATE.
            if emote_id in STATE_EMOTE_IDS:
                self.log.info(f"TEXT_EMOTE: state emote -> UNIT_NPC_EMOTESTATE")
                from src.world import object_update
                from src.world import update_fields as F
                update_payload = object_update.build_values_update(
                    sender_guid,
                    F.PLAYER_VALUES_COUNT - 1,
                    {F.UNIT_NPC_EMOTESTATE: emote_id}
                )
                await self.send_packet_to_peers(update_payload[0], update_payload[1])
            else:
                # Oneshot emotes (wave, bow, kiss, laugh, etc.) need SMSG_EMOTE
                # to play the animation on peers. The client's SMSG_EMOTE handler
                # calls PlayEmoteAnimation directly.
                self.log.info(f"TEXT_EMOTE: oneshot emote -> SMSG_EMOTE (emote_id={emote_id})")
                payload = login_packets.build_emote_payload(emote_id, sender_guid)
                await self.send_packet_to_peers(Opcodes.SMSG_EMOTE, payload)
                await self.send_packet(Opcodes.SMSG_EMOTE, payload)

    async def handle_emote(self, data: bytes):
        """Handle ``CMSG_EMOTE``: animation emote (no text, just an anim id).

        Wire format: uint32 emote_id.
        Server responds with SMSG_EMOTE: uint32 emote_id, uint64 guid.
        """
        if not self.in_world or not self.character:
            return

        if len(data) < 4:
            return
        emote_id = struct.unpack_from("<I", data, 0)[0]

        sender_guid = int(self.player_guid)

        self.log.info(f"EMOTE: emote_id={emote_id} sender_guid={sender_guid}")

        # Broadcast to peers so they see the animation
        payload = login_packets.build_emote_payload(emote_id, sender_guid)
        await self.send_packet_to_peers(Opcodes.SMSG_EMOTE, payload)
        await self.send_packet(Opcodes.SMSG_EMOTE, payload)

        # Also send a values update so the peer's UNIT_NPC_EMOTESTATE is updated
        # and its EmoteStateUpdateHandler triggers an animation refresh
        from src.world import object_update
        from src.world import update_fields as F
        update_payload = object_update.build_values_update(
            sender_guid,
            F.PLAYER_VALUES_COUNT - 1,
            {F.UNIT_NPC_EMOTESTATE: emote_id}
        )
        await self.send_packet_to_peers(update_payload[0], update_payload[1])

    async def handle_stand_state_change(self, data: bytes):
        """Handle ``CMSG_STANDSTATECHANGE``: client changes stand state (sit, kneel, sleep).

        Wire format: uint32 stand_state (0=stand, 1=sit, 3=kneel, 7=sleep, etc.).
        The state is stored in byte 0 of UNIT_FIELD_BYTES_1. We update our cached
        character and relay a VALUES update so other players see the stand state change.
        """
        if not self.in_world or not self.character:
            return

        if len(data) < 4:
            return
        await self._set_stand_state(struct.unpack_from("<I", data, 0)[0])

    async def _set_stand_state(self, stand_state: int) -> None:
        """Store byte 0 of ``UNIT_FIELD_BYTES_1`` and show it to everyone."""
        if not self.in_world or not self.character:
            return
        # Update the cached character's UNIT_FIELD_BYTES_1 byte 0
        from src.world import update_fields as F

        old_bytes1 = self.character.get("bytes1", 0)
        new_bytes1 = (old_bytes1 & 0xFFFFFF00) | (stand_state & 0xFF)
        self.character["bytes1"] = new_bytes1

        # Persist to DB
        await run_db_write(
            character_db.update_character_bytes,
            self.config.database.world_path, int(self.player_guid),
            "bytes1", new_bytes1,
        )

        # Send VALUES update to peers so they see the stand state change
        from src.world import object_update
        update_payload = object_update.build_values_update(
            self.player_guid,
            F.PLAYER_VALUES_COUNT - 1,
            {F.UNIT_FIELD_BYTES_1: new_bytes1}
        )
        await self.send_packet_to_peers(update_payload[0], update_payload[1])

        # Also send to self so our own client state is consistent
        await self.send_packet(update_payload[0], update_payload[1])

    async def _send_emote_animation(self, sender_guid: int, emote_text: str):
        """Send the emote animation packet for a chat emote.

        Maps the emote text (e.g., "wave", "shrug") to the client's EmotesText.dbc ID,
        then to the Emotes.dbc ID, and sends the appropriate packet:
        - State emotes (dance, sit, sleep, kneel): UNIT_NPC_EMOTESTATE VALUES update
        - Oneshot emotes (wave, bow, kiss, laugh): SMSG_EMOTE packet
        """
        text_emote_id = CHAT_EMOTE_TO_TEXT_ID.get(emote_text.lower())
        if not text_emote_id:
            self.log.debug(f"Unknown emote text: {emote_text}")
            return

        emote_id = TEXT_EMOTE_TO_EMOTE.get(text_emote_id, 0)
        if not emote_id:
            self.log.debug(f"No emote ID mapping for text_emote_id={text_emote_id}")
            return

        self.log.info(f"CHAT_EMOTE: text={emote_text} -> text_emote_id={text_emote_id} -> emote_id={emote_id}")

        # State emotes (dance, sit, sleep, kneel, etc.) use the NPC emote state
        # system via UNIT_NPC_EMOTESTATE.
        if emote_id in STATE_EMOTE_IDS:
            self.log.info(f"CHAT_EMOTE: state emote -> UNIT_NPC_EMOTESTATE")
            from src.world import object_update
            from src.world import update_fields as F
            update_payload = object_update.build_values_update(
                sender_guid,
                F.PLAYER_VALUES_COUNT - 1,
                {F.UNIT_NPC_EMOTESTATE: emote_id}
            )
            await self.send_packet_to_peers(update_payload[0], update_payload[1])
        else:
            # Oneshot emotes (wave, bow, kiss, laugh, etc.) need SMSG_EMOTE
            # to play the animation on peers. The client's SMSG_EMOTE handler
            # calls PlayEmoteAnimation directly.
            self.log.info(f"CHAT_EMOTE: oneshot emote -> SMSG_EMOTE (emote_id={emote_id})")
            payload = login_packets.build_emote_payload(emote_id, sender_guid)
            await self.send_packet_to_peers(Opcodes.SMSG_EMOTE, payload)
            await self.send_packet(Opcodes.SMSG_EMOTE, payload)

    async def handle_set_sheathed(self, data: bytes):
        """Handle ``CMSG_SET_SHEATHED``: client changes weapon drawn/sheath state.

        Wire format: uint32 sheath_state (0 = sheathed, 1 = melee drawn, 2 = ranged).
        The state is stored in byte 0 of UNIT_FIELD_BYTES_2.  We update our cached
        character and relay a VALUES update so other players see the weapon drawn.
        """
        if not self.in_world or not self.character:
            return

        if len(data) < 4:
            return
        sheath_state = struct.unpack_from("<I", data, 0)[0]

        # Update the cached character's UNIT_FIELD_BYTES_2 byte 0
        # The field is at update_fields.UNIT_FIELD_BYTES_2 (absolute index 122)
        from src.world import update_fields as F

        old_bytes2 = self.character.get("bytes2", 0)
        new_bytes2 = (old_bytes2 & 0xFFFFFF00) | (sheath_state & 0xFF)
        self.character["bytes2"] = new_bytes2

        # Persist to DB
        await run_db_write(
            character_db.update_character_bytes,
            self.config.database.world_path, int(self.player_guid),
            "bytes2", new_bytes2,
        )

        # Send VALUES update to peers so they see the weapon drawn/sheath change
        from src.world import object_update
        update_payload = object_update.build_values_update(
            self.player_guid,
            F.PLAYER_VALUES_COUNT - 1,  # include all fields
            {F.UNIT_FIELD_BYTES_2: new_bytes2}
        )
        await self.send_packet_to_peers(update_payload[0], update_payload[1])

        # Also send to self so our own client state is consistent
        await self.send_packet(update_payload[0], update_payload[1])
