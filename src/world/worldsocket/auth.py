"""Encrypted world authentication and account-data handlers."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import sqlite3
import struct

from src.crypto.world_crypt import WorldCrypt
from src.protocol.opcodes import Opcodes
from src.world import addon_info
from src.world import character_db
from src.world import login_packets

from .constants import (
    AUTH_FAILED,
    AUTH_OK,
    AUTH_UNKNOWN_ACCOUNT,
    AUTH_VERSION_MISMATCH,
)

class AuthMixin:
    async def send_auth_challenge(self):
        data = struct.pack("<I", 1)  # DOS zero bits
        data += self.server_challenge
        data += self.dos_challenge
        await self.send_packet(Opcodes.SMSG_AUTH_CHALLENGE, data)
        self.log.info("Sent SMSG_AUTH_CHALLENGE")

    def get_account(self, username: str):
        try:
            con = sqlite3.connect(self.config.database.auth_path)
            try:
                row = con.execute(
                    "SELECT id, username, session_key, expansion FROM account "
                    "WHERE UPPER(username) = ?",
                    (username.upper(),),
                ).fetchone()
            finally:
                con.close()
            return row
        except Exception as exc:
            self.log.error(f"Database error while loading account: {exc}")
            return None

    async def send_auth_error(self, code: int):
        await self.send_packet(Opcodes.SMSG_AUTH_RESPONSE, bytes([code]))
        self.should_close = True
        await asyncio.sleep(0.05)

    async def handle_auth_session(self, data: bytes):
        self.log.info(f"Received CMSG_AUTH_SESSION ({len(data)} bytes)")
        try:
            pos = 0
            build = struct.unpack_from("<I", data, pos)[0]
            pos += 4
            login_server_id = struct.unpack_from("<I", data, pos)[0]
            pos += 4
            account, pos = self._read_cstring(data, pos)
            login_server_type = struct.unpack_from("<I", data, pos)[0]
            pos += 4
            local_challenge = data[pos:pos + 4]
            pos += 4
            region_id = struct.unpack_from("<I", data, pos)[0]
            pos += 4
            battlegroup_id = struct.unpack_from("<I", data, pos)[0]
            pos += 4
            realm_id = struct.unpack_from("<I", data, pos)[0]
            pos += 4
            dos_response = struct.unpack_from("<Q", data, pos)[0]
            pos += 8
            digest = data[pos:pos + 20]
            pos += 20

            if len(local_challenge) != 4 or len(digest) != 20:
                await self.send_auth_error(AUTH_FAILED)
                return

            self.log.info(
                f"World auth: account='{account}' build={build} realm={realm_id} "
                f"loginServer={login_server_id} region={region_id} battlegroup={battlegroup_id}"
            )

            row = self.get_account(account)
            if not row:
                self.log.warning(f"Unknown account in world auth: '{account}'")
                await self.send_auth_error(AUTH_UNKNOWN_ACCOUNT)
                return

            account_id, stored_name, session_key, expansion = row
            if not session_key or len(session_key) != 40:
                self.log.warning(f"No valid 40-byte session key for '{account}' (log in via authserver first)")
                await self.send_auth_error(AUTH_FAILED)
                return

            self.session_key = bytes(session_key)
            self.account_id = account_id
            self.account_name = stored_name
            self.expansion = int(expansion) if expansion is not None else 2

            # The crypt is initialized before validating the digest so that the
            # client can actually decrypt an error response.
            self.crypt = WorldCrypt(self.session_key)

            expected = hashlib.sha1()
            expected.update(account.encode("utf-8"))
            expected.update(b"\x00\x00\x00\x00")
            expected.update(local_challenge)
            expected.update(self.server_challenge)
            expected.update(self.session_key)

            if not hmac.compare_digest(expected.digest(), digest):
                self.log.warning(f"World auth digest mismatch for account '{account}'")
                await self.send_auth_error(AUTH_FAILED)
                return

            if not (self.config.authserver.realm_build_min <= build <= self.config.authserver.realm_build_max):
                self.log.warning(f"Unsupported world client build {build}")
                await self.send_auth_error(AUTH_VERSION_MISMATCH)
                return

            # The rest of the packet is the client's secure-addon list.  It has
            # to be answered one-for-one or the client marks the Blizzard UI
            # addons as banned.
            try:
                self.addon_names = addon_info.parse_client_addon_info(data, pos)
            except addon_info.AddonInfoError as exc:
                self.log.warning(f"Could not read the client addon list: {exc}")
                self.addon_names = []
            self.log.info(f"Client announced {len(self.addon_names)} secure addon(s)")

            self.authenticated = True
            await self.send_auth_ok()
            await self.send_login_screen_packets()
            self.log.info(f"World session authenticated for '{self.account_name}'")

        except Exception as exc:
            self.log.exception(f"Auth session error: {exc}")
            await self.send_auth_error(AUTH_FAILED)

    async def send_auth_ok(self):
        payload = bytearray()
        payload.append(AUTH_OK)
        payload += struct.pack("<I", 0)  # billing time remaining
        payload.append(0)  # billing plan flags
        payload += struct.pack("<I", 0)  # billing time rested
        payload.append(self.expansion & 0xFF)
        await self.send_packet(Opcodes.SMSG_AUTH_RESPONSE, bytes(payload))

    async def send_login_screen_packets(self):
        # One record per secure addon the client announced, none banned.
        # Sending too few bytes makes the client mark the Blizzard UI addons
        # as banned and show a modal popup that blocks all input.
        await self.send_packet(
            Opcodes.SMSG_ADDON_INFO, addon_info.build_addon_info(self.addon_names)
        )
        # The client persists item stats in itemcache.wdb and only reloads an
        # entry when this version differs from the one stored in that file.
        # Hash the item templates instead of sending a constant, otherwise item
        # edits (and fixes like a weapon's sheath type) never reach the client.
        # The version is an aggregate over every item template (~100 ms on
        # Windows).  It cannot change while the server runs, so compute it
        # once off the event loop and cache it for later logins.
        cache_version = await asyncio.to_thread(
            character_db.client_cache_version, self.config.database.world_path
        )
        await self.send_packet(
            Opcodes.SMSG_CLIENTCACHE_VERSION, struct.pack("<I", cache_version)
        )
        self.log.info(f"Client cache version {cache_version} (item data hash)")
        await self.send_packet(
            Opcodes.SMSG_TUTORIAL_FLAGS, login_packets.build_tutorial_flags()
        )

    async def handle_realm_split(self, data: bytes):
        realm_id = struct.unpack_from("<I", data, 0)[0] if len(data) >= 4 else self.config.authserver.realm_id
        payload = struct.pack("<I", realm_id) + struct.pack("<I", 0) + b"01/01/01\x00"
        await self.send_packet(Opcodes.SMSG_REALM_SPLIT, payload)

    async def handle_ping(self, data: bytes):
        if len(data) >= 4:
            ping = struct.unpack_from("<I", data, 0)[0]
            await self.send_packet(Opcodes.SMSG_PONG, struct.pack("<I", ping))

    async def handle_request_account_data(self, data: bytes):
        """Answer ``CMSG_REQUEST_ACCOUNT_DATA`` with an empty blob.

        The client only asks when the server advertises a newer timestamp than
        its local cache.  ``build_account_data_times`` sends zero, so this is a
        safety net for clients that cached a newer time from another realm:
        replying with ``size = 0`` clears the pending download instead of
        leaving the UI (and its key bindings) stuck waiting forever.
        """
        data_type = struct.unpack_from("<I", data, 0)[0] if len(data) >= 4 else 0
        guid = self.player_guid or 0
        payload = (
            struct.pack("<Q", guid)
            + struct.pack("<I", data_type)
            + struct.pack("<I", 0)  # server time
            + struct.pack("<I", 0)  # uncompressed size: no data
        )
        await self.send_packet(Opcodes.SMSG_UPDATE_ACCOUNT_DATA, payload)

    async def handle_update_account_data(self, data: bytes):
        """Accept an account-data upload (bindings, config, macros, chat)."""
        data_type = struct.unpack_from("<I", data, 0)[0] if len(data) >= 4 else 0
        size = struct.unpack_from("<I", data, 8)[0] if len(data) >= 12 else 0
        self.log.debug(
            f"account data upload: type={data_type} compressed={size} bytes"
        )
        await self.send_packet(
            Opcodes.SMSG_UPDATE_ACCOUNT_DATA_COMPLETE,
            struct.pack("<II", data_type, 0),
        )
