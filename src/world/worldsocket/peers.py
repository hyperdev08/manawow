"""Visibility and relay between players in the world."""
from __future__ import annotations

import struct

from src.protocol.opcodes import Opcodes
from src.world import character_db
from src.world import login_packets
from src.world import object_update

class PeersMixin:
    def peers(self) -> list["WorldSocket"]:
        """Return the in-world sessions that should see this character.

        A 3.3.5 client only knows about objects the server tells it about, so
        every session has to be told when another player enters, moves or
        leaves.  Only sessions on the same map are considered; there is no
        zone/instance splitting yet.
        """
        if self.server is None or not self.in_world or not self.character:
            return []
        my_map = int(self.character.get("map", 0))
        result = []
        for session in list(getattr(self.server, "sessions", ())):
            if session is self:
                continue
            if not getattr(session, "in_world", False):
                continue
            # A client that has not finished its entry burst yet must not get
            # update objects before SMSG_LOGIN_VERIFY_WORLD / its own create.
            if not getattr(session, "world_entry_complete", False):
                continue
            peer_character = getattr(session, "character", None)
            if not peer_character or not getattr(session, "player_guid", None):
                continue
            if int(peer_character.get("map", 0)) != my_map:
                continue
            writer = getattr(session, "writer", None)
            if writer is None or writer.is_closing():
                continue
            result.append(session)
        return result

    async def send_packet_to_peers(self, opcode: int, payload: bytes = b""):
        """Broadcast one packet to every peer, ignoring dead writers."""
        for peer in self.peers():
            await peer.send_packet(opcode, payload)

    async def announce_self_to_peers(self):
        """Send this character's create block to every other player."""
        if not self.character:
            return
        peers = self.peers()
        if not peers:
            return
        opcode, payload = object_update.build_update_object(
            self.character, self_player=False
        )
        for peer in peers:
            await peer.send_packet(opcode, payload)
            # Also send name query response so the peer knows our name
            name_payload = login_packets.build_name_query_response(self.character)
            await peer.send_packet(Opcodes.SMSG_NAME_QUERY_RESPONSE, name_payload)
        self.log.info(
            f"Announced '{self.character['name']}' (guid {self.player_guid}) "
            f"to {len(peers)} other player(s)"
        )

    async def send_peer_spawns(self):
        """Send create blocks for the players already in the world."""
        peers = self.peers()
        for peer in peers:
            opcode, payload = object_update.build_update_object(
                peer.character, self_player=False
            )
            await self.send_packet(opcode, payload)
            # Also send name query response so we know the peer's name
            name_payload = login_packets.build_name_query_response(peer.character)
            await self.send_packet(Opcodes.SMSG_NAME_QUERY_RESPONSE, name_payload)
            # Pets/guardians that already exist are session-local to their
            # owner, so the newcomer needs their create blocks separately.
            await peer.send_summons_to(self)
        if peers:
            self.log.info(
                f"Sent {len(peers)} existing player(s) to '{self.character['name']}'"
            )

    async def despawn_for_peers(self):
        """Remove this character from the other clients (logout/disconnect)."""
        if self._despawn_sent or not self.in_world or not self.player_guid:
            return
        self._despawn_sent = True
        peers = self.peers()
        if not peers:
            return
        payload = login_packets.build_destroy_object(int(self.player_guid))
        for peer in peers:
            await peer.send_packet(Opcodes.SMSG_DESTROY_OBJECT, payload)
        if self.character:
            self.log.info(
                f"Removed '{self.character['name']}' (guid {self.player_guid}) "
                f"from {len(peers)} other player(s)"
            )

    async def handle_get_mirrorimage_data(self, data: bytes):
        """``CMSG_GET_MIRRORIMAGE_DATA``: a client wants another player's look.

        Peer create blocks set ``UNIT_FLAG2_MIRROR_IMAGE``, so the 3.3.5 client
        sends this request (uint64 guid) once the peer's model has loaded
        instead of building the armour from ``PLAYER_VISIBLE_ITEM_*`` itself.
        We answer with ``SMSG_MIRRORIMAGE_DATA`` so its handler can allocate the
        character component and add all 11 armour slots; weapons still come
        from the visible-item fields through ``AddHandItem``.
        """
        if not self.in_world or len(data) < 8:
            return
        guid = struct.unpack_from("<Q", data, 0)[0]

        target = self if guid == self.player_guid else None
        if target is None:
            for session in getattr(self.server, "sessions", ()):
                if getattr(session, "in_world", False) and \
                   getattr(session, "player_guid", None) == guid:
                    target = session
                    break
        if target is None or not target.character:
            self.log.debug(
                f"CMSG_GET_MIRRORIMAGE_DATA for unknown guid {guid} ignored"
            )
            return

        # Read the inventory back from SQLite: it is the source of truth for the
        # target's equipment and already carries each item's display id.
        inventory = character_db.get_character_inventory(
            self.config.database.world_path, guid
        )
        payload = login_packets.build_mirror_image_data(target.character, inventory)
        await self.send_packet(Opcodes.SMSG_MIRRORIMAGE_DATA, payload)

    async def broadcast_equipment_update(self, force_uncompressed: bool = True):
        """Send the equipment/bag fields to this player *and* to its peers.

        The client refreshes a player's model only when ``PLAYER_VISIBLE_ITEM_*``
        changes, so after any swap the same ``UPDATETYPE_VALUES`` update has to
        reach every client that can see this character, not just the owner.
        """
        if not self.in_world or not self.character:
            return
        changed = object_update.build_equipment_values(
            self.character.get("inventory", {})
        )
        # Equipment also changes attributes, health/mana, armor and attack
        # power; send those in the same values update so the character sheet
        # and the bars refresh immediately.
        changed.update(
            object_update.build_combat_stats_values(self.character)
        )
        opcode, payload = object_update.build_player_values_update(
            self.character, changed, force_uncompressed=force_uncompressed
        )
        await self.send_packet(opcode, payload)
        await self.send_packet_to_peers(opcode, payload)
