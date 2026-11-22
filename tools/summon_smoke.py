#!/usr/bin/env python3
"""Headless smoke test for summoning spells (pets and guardians).

Runs against the real ``data/world.db`` and drives the summon mixin the same
way :meth:`WorldSocket._apply_spell_effects` does, without a game client.
"""
from __future__ import annotations

import asyncio
import os
import sqlite3
import sys
import tempfile
import types

root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, root)

from src.protocol.opcodes import Opcodes
from src.world import creature
from src.world import object_update
from src.world import spells
from src.world import update_fields as F
from src.world.realmsocket import WorldSocket
from src.world.worldsocket.summons import SummonsMixin


WORLD_DB = os.path.join(root, "data", "world.db")


def assert_eq(actual, expected, label: str) -> None:
    if actual != expected:
        raise AssertionError(f"{label}: expected {expected!r}, got {actual!r}")
    print(f"  {label}: {actual!r}")


class FakeSocket(SummonsMixin):
    """Just enough of a WorldSocket for the summon mixin."""

    def __init__(self) -> None:
        self.config = types.SimpleNamespace(
            database=types.SimpleNamespace(world_path=WORLD_DB))
        self.character = {
            "name": "Demo", "class": 9, "race": 1, "level": 3,
            "map": 0, "zone": 12,
            "x": 0.0, "y": 0.0, "z": 0.0, "o": 0.0,
            "health": 100, "max_health": 100, "power": 100, "max_power": 100,
        }
        self.player_guid = 0x0000000000000042
        self.in_world = True
        self.log = types.SimpleNamespace(
            debug=lambda *a, **k: None,
            info=lambda *a, **k: None,
            exception=lambda *a, **k: None,
        )
        self.sent: list[tuple[int, bytes]] = []
        self.peer_sent: list[tuple[int, bytes]] = []
        self._visible_creatures: dict[int, int] = {}
        self._creature_spawn_cache: dict[int, object] = {}
        self._summoned_creatures: dict[int, dict] = {}
        self._summon_states: dict[int, dict] = {}
        self._summon_serial = 0
        self._next_move = 100
        self.damage: list[tuple[int, int, int]] = []
        self.attacker_updates: list[tuple[int, int, int]] = []
        self.fake_spawns: dict[int, dict] = {}
        self._combat_state = {
            "dead": False,
            "in_combat": False,
            "auto_attacking": False,
            "target_guid": 0,
            "selected_guid": 0,
            "aggro": {},
            "idle": {},
            "idle_settle": {},
        }

    # -- WorldSocket helpers the mixin calls -------------------------------
    def _get_combat_state(self):
        return self._combat_state

    def _player_position(self):
        return (float(self.character["x"]), float(self.character["y"]),
                float(self.character["z"]))

    def _get_creature_spawn(self, low_guid):
        if int(low_guid) in self._summoned_creatures:
            return self._summoned_creatures[int(low_guid)]
        return self.fake_spawns.get(int(low_guid))

    def _creature_position(self, low_guid):
        spawn = self._get_creature_spawn(low_guid)
        return (float(spawn["x"]), float(spawn["y"]), float(spawn["z"]))

    def _creature_object_guid(self, low_guid, entry=0):
        if not entry:
            spawn = self._get_creature_spawn(low_guid)
            entry = int(spawn["entry"])
        return creature.creature_guid(int(low_guid), int(entry))

    def _next_move_id(self):
        self._next_move += 1
        return self._next_move

    def _melee_reach(self, spawn):
        return WorldSocket._melee_reach(self, spawn)

    def _creature_damage(self, spawn):
        return WorldSocket._creature_damage(self, spawn)

    def _creature_max_health(self, spawn):
        return WorldSocket._creature_max_health(self, spawn)

    def _distance(self, a, b):
        return WorldSocket._distance(a, b)

    async def _send_creature_fields(self, low_guid, fields):
        return None

    async def _send_attacker_state_update(self, attacker, victim, damage,
                                          **kwargs):
        self.attacker_updates.append((int(attacker), int(victim), int(damage)))

    async def _deal_damage_to_creature(self, guid, damage, attacker_guid=0):
        self.damage.append((int(guid), int(damage), int(attacker_guid)))
        return False

    async def send_packet(self, opcode, payload=b""):
        self.sent.append((int(opcode), bytes(payload)))

    async def send_packet_to_peers(self, opcode, payload=b""):
        self.peer_sent.append((int(opcode), bytes(payload)))


async def main() -> None:
    if not os.path.exists(WORLD_DB):
        print("data/world.db missing; cannot run the summon smoke test")
        raise SystemExit(1)

    # -- spell data --------------------------------------------------------
    imp = spells.get_spell_template(WORLD_DB, 688)
    assert imp is not None, "Summon Imp (688) missing from spell_templates"
    assert_eq(imp.name, "Summon Imp", "spell name")
    assert_eq(imp.summon_entries(), [(416, True)], "Summon Imp -> imp pet")
    assert_eq(spells.summon_duration_ms(WORLD_DB, imp.duration_index, 3), -1,
              "warlock pets last until dismissed")

    call_pet = spells.get_spell_template(WORLD_DB, 883)
    assert call_pet is not None, "Call Pet (883) missing"
    assert_eq(call_pet.summon_entries(), [(0, True)],
              "Call Pet has no creature entry without stored pet state")

    # Totem spells are stationary summons; they must not be driven by the pet
    # follow/assist AI.
    searing = spells.get_spell_template(WORLD_DB, 3599)
    assert searing is not None, "Searing Totem (3599) missing"
    assert_eq(searing.is_totem(), True, "totem spells carry SPELL_ATTR_TOTEM")

    # An 18 s timed summon from the fallback table (the ``spell_duration``
    # table is filled from SpellDuration.dbc by the world importer).
    assert_eq(spells.summon_duration_ms(WORLD_DB, 18, 1), 18000,
              "fallback duration for index 18")
    assert_eq(spells.summon_duration_ms(WORLD_DB, 22, 1), 30000,
              "fallback duration for index 22")

    # A real ``spell_duration`` row: duration + per-level, capped by max.
    handle, temp_db = tempfile.mkstemp(suffix=".db")
    os.close(handle)
    con = sqlite3.connect(temp_db)
    con.execute(
        "CREATE TABLE spell_duration(id INTEGER PRIMARY KEY, duration INTEGER,"
        " duration_per_level INTEGER, max_duration INTEGER)")
    con.execute("INSERT INTO spell_duration VALUES(18, 20000, 1000, 30000)")
    con.execute("INSERT INTO spell_duration VALUES(5, -1, 0, 0)")
    con.commit()
    con.close()
    try:
        assert_eq(spells.summon_duration_ms(temp_db, 18, 5), 24000,
                  "SpellDuration per-level value is capped by max_duration")
        assert_eq(spells.summon_duration_ms(temp_db, 5, 5), -1,
                  "a negative SpellDuration means until cancelled")
    finally:
        os.unlink(temp_db)

    sock = FakeSocket()

    # -- summon ------------------------------------------------------------
    low_guid = await sock._summon_creature(416, -1, is_pet=True)
    assert low_guid is not None, "summon created"
    spawn = sock._summoned_creatures[low_guid]
    assert_eq(spawn["entry"], 416, "summon entry")
    assert_eq(spawn["summoned_by"], sock.player_guid, "summon owner guid")
    assert_eq(spawn["min_level"], 3, "summon takes the owner's level")
    assert spawn["current_health"] > 0, "summon has health"
    assert_eq(low_guid >= 0xE00000, True, "summon guid in the runtime range")
    assert_eq(sock._visible_creatures[low_guid],
              creature.creature_guid(low_guid, 416), "summon is visible")
    assert_eq(sock._creature_spawn_cache[low_guid] is spawn, True,
              "summon is in the spawn cache")

    opcode, payload = sock.sent[-1]
    assert opcode in (int(Opcodes.SMSG_UPDATE_OBJECT),
                      int(Opcodes.SMSG_COMPRESSED_UPDATE_OBJECT)), \
        "summon announces a create object"
    assert sock.peer_sent and sock.peer_sent[-1][0] == opcode, \
        "summon is broadcast to peers"

    # The update block must carry the owner in SUMMONEDBY/CREATEDBY and the
    # player-controlled flag; a warlock pet with the imp's wild 0x8000 flag
    # would be unselectable for its owner.
    values = object_update.build_creature_values(spawn)
    assert_eq(values[F.UNIT_FIELD_SUMMONEDBY], sock.player_guid & 0xFFFFFFFF,
              "UNIT_FIELD_SUMMONEDBY low word")
    assert_eq(values[F.UNIT_FIELD_SUMMONEDBY + 1],
              (sock.player_guid >> 32) & 0xFFFFFFFF,
              "UNIT_FIELD_SUMMONEDBY high word")
    assert_eq(values[F.UNIT_FIELD_CREATEDBY], sock.player_guid & 0xFFFFFFFF,
              "UNIT_FIELD_CREATEDBY low word")
    assert_eq(values[F.UNIT_FIELD_FLAGS], 0x00000008,
              "summon is player-controlled")

    # -- assist in combat --------------------------------------------------
    enemy_guid = creature.creature_guid(7, 1009)
    sock.fake_spawns[7] = {
        "guid": 7, "entry": 1009, "name": "Mosshide Mistweaver",
        "x": 1.0, "y": 0.0, "z": 0.0, "o": 0.0, "dead_since": 0,
        "combat_reach": 1.5, "min_level": 22, "max_level": 22,
        "base_health": 1000, "hp_multiplier": 1.0,
    }
    sock._combat_state.update({
        "in_combat": True,
        "auto_attacking": True,
        "target_guid": enemy_guid,
    })
    spawn["x"], spawn["y"], spawn["z"] = 0.5, 0.0, 0.0
    sock._summon_state(low_guid)["last_swing"] = 0.0
    await sock._summon_tick_all(100.0)
    assert sock.attacker_updates, "pet swings at the owner's target"
    pet_attack = sock.attacker_updates[-1]
    assert_eq(pet_attack[0], creature.creature_guid(low_guid, 416),
              "pet is the attacker")
    assert_eq(pet_attack[1], enemy_guid, "pet attacks the selected enemy")
    assert sock.damage and sock.damage[-1][0] == enemy_guid, \
        "pet damage reaches the creature"

    # A swing inside the 2 s interval must not repeat.
    swings = len(sock.attacker_updates)
    await sock._summon_tick_all(100.5)
    assert_eq(len(sock.attacker_updates), swings, "pet respects its swing timer")

    # -- follow ------------------------------------------------------------
    sock._combat_state.update({"in_combat": False, "auto_attacking": False,
                               "target_guid": 0, "aggro": {}})
    sock.character["x"], sock.character["y"], sock.character["z"] = 20.0, 0.0, 0.0
    before = (spawn["x"], spawn["y"], spawn["z"])
    await sock._summon_tick_all(200.0)
    moved = (spawn["x"], spawn["y"], spawn["z"])
    assert moved[0] > before[0], "pet walks towards a distant owner"
    assert sock.sent[-1][0] == int(Opcodes.SMSG_MONSTER_MOVE), \
        "follow uses SMSG_MONSTER_MOVE"
    assert sock.peer_sent[-1][0] == int(Opcodes.SMSG_MONSTER_MOVE), \
        "follow is relayed to peers"

    # -- expiry ------------------------------------------------------------
    state = sock._summon_state(low_guid)
    state["expires"] = 0.0
    await sock._summon_tick_all(300.0)
    assert_eq(low_guid in sock._summoned_creatures, False,
              "expired summon is removed")
    assert_eq(sock.sent[-1][0], int(Opcodes.SMSG_DESTROY_OBJECT),
              "expiry sends SMSG_DESTROY_OBJECT")

    # -- replacement and logout dismissal ----------------------------------
    first = await sock._summon_creature(416, -1)
    second = await sock._summon_creature(416, -1)
    assert first is not None and second is not None
    assert_eq(sorted(sock._summoned_creatures), [second],
              "a new summon replaces the previous pet")
    await sock.dismiss_all_summons()
    assert_eq(sock._summoned_creatures, {}, "dismiss_all_summons clears the pet")
    assert_eq(sock._visible_creatures, {}, "dismiss removes visibility state")

    print("summon smoke OK")


if __name__ == "__main__":
    asyncio.run(main())
