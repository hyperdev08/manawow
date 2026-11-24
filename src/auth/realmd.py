"""Authserver (logon/realmd) protocol handler for WoW 3.3.5a."""
from __future__ import annotations

import asyncio
import sqlite3
import struct

from src.common.config import Config
from src.common.db_io import execute_write, run_db_write
from src.common.logger import setup_logger
from src.crypto.srp6 import SRP6
from src.auth.realm_list import Realm, build_realm_list
from src.network.packet_log import get_packet_log, Direction


CMD_AUTH_LOGON_CHALLENGE = 0x00
CMD_AUTH_LOGON_PROOF = 0x01
CMD_REALM_LIST = 0x10

WOW_SUCCESS = 0x00
WOW_FAIL_UNKNOWN_ACCOUNT = 0x04
WOW_FAIL_INCORRECT_PASSWORD = 0x05
WOW_FAIL_VERSION_INVALID = 0x09
WOW_FAIL_DB_BUSY = 0x08

VERSION_CHALLENGE = bytes.fromhex("BAA31E99A00B2157FC373FB369CDD2F1")

CHALLENGE_BODY_SIZE = 30  # fixed fields before the username
PROOF_PACKET_SIZE = 74  # A(32) + M1(20) + crc(20) + keys(1) + flags(1)


class AuthSession:
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, config: Config):
        self.reader = reader
        self.writer = writer
        self.config = config
        self.log = setup_logger("realmd", config.logging.log_dir, config.logging.level, config.logging.console)
        self.packet_log = get_packet_log()

        self.account_name = None
        self.account_id = None
        self.salt = None
        self.verifier = None
        self.B = None
        self.b = None
        self.A = None
        self.session_key = None
        self.M2 = None

    # ------------------------------------------------------------------
    # I/O helpers
    # ------------------------------------------------------------------
    async def send(self, data: bytes):
        try:
            if self.packet_log.CanLogPacket() and len(data) > 0:
                self.packet_log.LogPacket(data[1:], data[0], Direction.SERVER_TO_CLIENT)
            self.writer.write(data)
            await self.writer.drain()
        except Exception as exc:  # client disappeared
            self.log.debug(f"Send error: {exc}")

    async def handle(self):
        try:
            while True:
                cmd = (await asyncio.wait_for(self.reader.readexactly(1), timeout=300))[0]
                if cmd == CMD_AUTH_LOGON_CHALLENGE:
                    await self.handle_logon_challenge()
                elif cmd == CMD_AUTH_LOGON_PROOF:
                    await self.handle_logon_proof()
                elif cmd == CMD_REALM_LIST:
                    await self.handle_realm_list()
                else:
                    self.log.debug(f"Unknown auth command: 0x{cmd:02X}")
                    break
        except (asyncio.IncompleteReadError, asyncio.TimeoutError,
                ConnectionError) as exc:
            self.log.debug(f"Auth client disconnected: {exc}")
        except Exception as exc:
            self.log.exception(f"Auth session error: {exc}")
        finally:
            try:
                self.writer.close()
                await self.writer.wait_closed()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Database
    # ------------------------------------------------------------------
    def get_account(self, username: str):
        try:
            con = sqlite3.connect(self.config.database.auth_path)
            try:
                cur = con.cursor()
                cur.execute(
                    "SELECT id, username, v, s, expansion FROM account WHERE UPPER(username) = ?",
                    (username.upper(),),
                )
                row = cur.fetchone()
            finally:
                con.close()
            return row
        except Exception as exc:
            self.log.error(f"Database error while loading account: {exc}")
            return None

    async def store_session_key(self, account_id: int, session_key: bytes, ip: str):
        try:
            await run_db_write(
                execute_write, self.config.database.auth_path,
                "UPDATE account SET session_key = ?, last_login = CURRENT_TIMESTAMP, "
                "last_ip = ?, failed_logins = 0 WHERE id = ?",
                (session_key, ip, account_id),
            )
        except Exception as exc:
            self.log.error(f"Failed to store session key: {exc}")

    def get_character_count(self, account_id: int) -> int:
        try:
            path = (getattr(self.config.database, "character_path", "")
                    or self.config.database.world_path)
            con = sqlite3.connect(path)
            try:
                row = con.execute(
                    "SELECT COUNT(*) FROM characters WHERE account_id = ?", (account_id,)
                ).fetchone()
            finally:
                con.close()
            return int(row[0]) if row else 0
        except Exception:
            return 0

    # ------------------------------------------------------------------
    # AUTH_LOGON_CHALLENGE
    # ------------------------------------------------------------------
    async def handle_logon_challenge(self):
        try:
            prefix = await self.reader.readexactly(3)
            size = struct.unpack("<H", prefix[1:3])[0]
            if size < CHALLENGE_BODY_SIZE or size > CHALLENGE_BODY_SIZE + 255:
                self.log.warning(f"Malformed logon challenge size: {size}")
                return

            body = await self.reader.readexactly(size)
            build = struct.unpack("<H", body[7:9])[0]
            username_len = body[29]
            if size != CHALLENGE_BODY_SIZE + username_len:
                self.log.warning("Logon challenge size does not match username length")
                return

            self.account_name = body[30:30 + username_len].decode("utf-8", errors="replace")
            self.log.info(f"Auth challenge for account '{self.account_name}' (build {build})")

            if not (self.config.authserver.realm_build_min <= build <= self.config.authserver.realm_build_max):
                self.log.warning(f"Rejected unsupported client build {build}")
                await self.send(self._challenge_failure(WOW_FAIL_VERSION_INVALID))
                return

            account = self.get_account(self.account_name)
            if not account:
                self.log.warning(f"Account not found: {self.account_name}")
                await self.send(self._challenge_failure(WOW_FAIL_UNKNOWN_ACCOUNT))
                return

            self.account_id, stored_name, v_blob, s_blob, _expansion = account
            self.account_name = stored_name

            if not v_blob or not s_blob or len(v_blob) != 32 or len(s_blob) != 32:
                self.log.warning(f"Account '{self.account_name}' has no valid SRP6 data")
                await self.send(self._challenge_failure(WOW_FAIL_UNKNOWN_ACCOUNT))
                return

            self.salt = bytes(s_blob)
            self.verifier = int.from_bytes(bytes(v_blob), "little")
            self.b, self.B = SRP6.generate_challenge(self.verifier)

            # 3.3.5a AUTH_LOGON_CHALLENGE_S layout:
            #     uint8 cmd
            #     uint8 unknown (always 0)
            #     uint8 error
            #     uint8 B[32]
            #     uint8 g_len, uint8 g, uint8 N_len, uint8 N[32]
            #     uint8 s[32]
            #     uint8 versionChallenge[16]
            #     uint8 securityFlags
            # The "unknown" byte is easy to miss but the client parses B after it;
            # omitting it shifts every following field by one byte and the client
            # rejects the challenge without ever sending AUTH_LOGON_PROOF.
            response = bytearray()
            response.append(CMD_AUTH_LOGON_CHALLENGE)
            response.append(0x00)  # unknown
            response.append(WOW_SUCCESS)
            response += self.B
            response.append(1)
            response.append(SRP6.g)
            response.append(32)
            response += SRP6.N_BYTES
            response += self.salt
            response += VERSION_CHALLENGE
            response.append(0)  # security flags

            await self.send(bytes(response))
            self.log.debug(f"Sent SRP6 challenge ({len(response)} bytes)")

        except Exception as exc:
            self.log.exception(f"Logon challenge error: {exc}")

    # ------------------------------------------------------------------
    # AUTH_LOGON_PROOF
    # ------------------------------------------------------------------
    @staticmethod
    def _challenge_failure(error: int) -> bytes:
        # Every challenge response carries the extra "unknown" byte, including
        # error replies: cmd, unknown(0), error.
        return bytes([CMD_AUTH_LOGON_CHALLENGE, 0x00, error])

    async def handle_logon_proof(self):
        failed = bytes([CMD_AUTH_LOGON_PROOF, WOW_FAIL_UNKNOWN_ACCOUNT, 0x00, 0x00])
        try:
            data = await self.reader.readexactly(PROOF_PACKET_SIZE)
            self.log.info(f"Received logon proof from '{self.account_name}'")
            if not all((self.B, self.b, self.salt, self.verifier, self.account_name)):
                await self.send(failed)
                return

            a_bytes = data[0:32]
            client_m1 = data[32:52]
            self.A = SRP6.from_le(a_bytes)

            ok, m2, session_key = SRP6.verify_proof(
                self.account_name,
                a_bytes,
                self.B,
                self.b,
                self.verifier,
                self.salt,
                client_m1,
            )

            if not ok:
                self.log.warning(f"SRP6 proof failed for '{self.account_name}' (wrong password?)")
                await self.send(failed)
                return

            self.M2 = m2
            self.session_key = session_key

            peer = self.writer.get_extra_info("peername")
            ip = peer[0] if peer else "127.0.0.1"
            await self.store_session_key(self.account_id, session_key, ip)

            response = bytearray()
            response.append(CMD_AUTH_LOGON_PROOF)
            response.append(WOW_SUCCESS)
            response += m2
            response += struct.pack("<I", 0x00800000)  # account flags
            response += struct.pack("<I", 0)  # survey id
            response += struct.pack("<H", 0)  # login flags

            await self.send(bytes(response))
            self.log.info(f"SRP6 authentication successful for '{self.account_name}'")

        except asyncio.IncompleteReadError as exc:
            self.log.warning(
                f"Logon proof truncated: got {len(exc.partial)} of {PROOF_PACKET_SIZE} bytes"
            )
            await self.send(failed)
        except Exception as exc:
            self.log.exception(f"Logon proof error: {exc}")
            await self.send(failed)

    # ------------------------------------------------------------------
    # REALM_LIST
    # ------------------------------------------------------------------
    async def handle_realm_list(self):
        try:
            # Client sends cmd + uint32 (build/unknown).  Ignore the value.
            try:
                await self.reader.readexactly(4)
            except asyncio.IncompleteReadError:
                pass

            characters = self.get_character_count(self.account_id) if self.account_id else 0

            realm = Realm(
                name=self.config.authserver.realm_name,
                address=self.config.authserver.realm_address,
                port=self.config.worldserver.port,
                realm_type=self.config.authserver.realm_type,
                flags=self.config.authserver.realm_flags,
                population=self.config.authserver.realm_population,
                timezone=self.config.authserver.realm_timezone,
                build=self.config.authserver.realm_build_max,
                realm_id=self.config.authserver.realm_id,
                characters=characters,
            )

            packet = build_realm_list([realm])
            await self.send(packet)
            self.log.info(
                f"Sent realm list: '{realm.name}' at {realm.address}:{realm.port} "
                f"({characters} character(s))"
            )
        except Exception as exc:
            self.log.exception(f"Realm list error: {exc}")
