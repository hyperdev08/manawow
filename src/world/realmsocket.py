"""Worldserver socket handler for WoW 3.3.5a (build 12340).

``WorldSocket`` is assembled from the domain mixins in
:mod:`src.world.worldsocket`; this module keeps the historical import path and
re-exports the constants and formulas the rest of the server (and the protocol
self-test) expect:

SMSG_AUTH_CHALLENGE -> CMSG_AUTH_SESSION -> SMSG_AUTH_RESPONSE ->
SMSG_ADDON_INFO / SMSG_CLIENTCACHE_VERSION / SMSG_TUTORIAL_FLAGS ->
CMSG_CHAR_ENUM / CMSG_CHAR_CREATE / CMSG_CHAR_DELETE ->
CMSG_PLAYER_LOGIN -> SMSG_LOGIN_VERIFY_WORLD + initial packets + self
create-object -> MSG_MOVE_* relayed to the other in-world players ->
CMSG_LOGOUT_REQUEST back to character selection.
"""
from __future__ import annotations

from src.protocol.opcodes import Opcodes
from src.world.worldsocket.auth import AuthMixin
from src.world.worldsocket.auras import AurasMixin
from src.world.worldsocket.base import SocketBase
from src.world.worldsocket.characters import CharactersMixin
from src.world.worldsocket.chat import ChatMixin
from src.world.worldsocket.combat import CombatMixin
from src.world.worldsocket.constants import *  # noqa: F401,F403
from src.world.worldsocket.constants import __all__ as _CONSTANT_NAMES
from src.world.worldsocket.creature_ai import CreatureAIMixin
from src.world.worldsocket.creature_movement import CreatureMovementMixin
from src.world.worldsocket.creatures import CreaturesMixin
from src.world.worldsocket.inventory import InventoryMixin
from src.world.worldsocket.loot import LootMixin
from src.world.worldsocket.peers import PeersMixin
from src.world.worldsocket.player_movement import PlayerMovementMixin
from src.world.worldsocket.runes import RunesMixin
from src.world.worldsocket.spells import SpellsMixin
from src.world.worldsocket.stats import StatsMixin
from src.world.worldsocket.summons import SummonsMixin
from src.world.worldsocket.world_entry import WorldEntryMixin

__all__ = ["WorldSocket", "Opcodes", *_CONSTANT_NAMES]


class WorldSocket(
    SocketBase,
    AuthMixin,
    PeersMixin,
    CharactersMixin,
    WorldEntryMixin,
    PlayerMovementMixin,
    ChatMixin,
    StatsMixin,
    AurasMixin,
    RunesMixin,
    SpellsMixin,
    CombatMixin,
    CreaturesMixin,
    CreatureAIMixin,
    CreatureMovementMixin,
    SummonsMixin,
    LootMixin,
    InventoryMixin,
):
    """One connected 3.3.5a client; see the mixins for each subsystem."""
