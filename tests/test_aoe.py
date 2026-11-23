"""AoE spells: caster/destination areas, cones, chains and ground ticks.

The tests drive the real ``SpellTemplate`` and ``SpellRadius`` data from
``data/world.db`` against a small fake session and a handful of creatures.
"""
from __future__ import annotations

import asyncio
import os
import struct
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src.world import creature, spells
from src.world.realmsocket import WorldSocket
from src.world.worldsocket.creature_ai import CreatureAIMixin
from src.world.worldsocket.spells import SpellsMixin

WORLD_DB = os.path.join(ROOT, "data", "world.db")


def _async_test(fn):
    """Run an async test body without pytest-asyncio."""
    def wrapper(*args, **kwargs):
        return asyncio.run(fn(*args, **kwargs))
    wrapper.__name__ = fn.__name__
    return wrapper


class FakeSession(SpellsMixin):
    def __init__(self):
        self.config = types.SimpleNamespace(
            database=types.SimpleNamespace(world_path=WORLD_DB))
        self.character = {
            "name": "Aoe", "class": 8, "race": 1, "level": 20,
            "x": 0.0, "y": 0.0, "z": 0.0, "o": 0.0,
            "health": 1000, "max_health": 1000, "power": 1000,
            "max_power": 1000,
        }
        self.player_guid = 0x42
        self.in_world = True
        self.log = types.SimpleNamespace(
            debug=lambda *a, **k: None, info=lambda *a, **k: None,
            exception=lambda *a, **k: None)
        self._summoned_creatures = {}
        self._spawns = {}
        self._creature_spawn_cache = self._spawns
        self.damage = []
        self.player_damage = []
        self._combat_state = {
            "dead": False, "dots": [], "area_effects": [],
            "combo_target": 0, "combo_points": 0,
            "pending_cast": None, "cooldowns": {}, "gcd_until": 0.0,
        }

    def add_creature(self, low, x, y=0.0, z=0.0):
        self._spawns[low] = {
            "guid": low, "entry": 1000 + low, "x": x, "y": y, "z": z,
            "o": 0.0, "dead_since": 0, "combat_reach": 1.5,
            "min_level": 20, "base_health": 500, "hp_multiplier": 1.0,
        }
        return creature.creature_guid(low, 1000 + low)

    def _get_combat_state(self):
        return self._combat_state

    def _get_creature_spawn(self, low):
        return self._spawns.get(int(low))

    def _creature_position(self, low):
        spawn = self._spawns[int(low)]
        return (float(spawn["x"]), float(spawn["y"]), float(spawn["z"]))

    def _creature_object_guid(self, low, entry=0):
        if not entry:
            entry = int(self._spawns[int(low)]["entry"])
        return creature.creature_guid(int(low), int(entry))

    def _distance(self, a, b):
        return WorldSocket._distance(a, b)

    @staticmethod
    def _read_packed_guid(data, pos):
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

    def _player_position(self):
        return (float(self.character["x"]), float(self.character["y"]),
                float(self.character["z"]))

    def _player_weapon_stats(self):
        return {"min": 10.0, "max": 20.0, "delay": 2.0}

    def _combo_target(self):
        return int(self._combat_state.get("combo_target") or 0)

    def _combo_points(self):
        return int(self._combat_state.get("combo_points") or 0)

    def _is_player_guid(self, guid):
        return int(guid) == self.player_guid

    def _creature_damage(self, spawn):
        return WorldSocket._creature_damage(self, spawn)

    def _create_mana(self):
        return 100

    def _is_death_knight(self):
        return False

    def _max_power(self):
        return int(self.character["max_power"])

    def _note_rune_prediction(self, template):
        return None

    async def _set_player_power(self, power):
        self.character["power"] = int(power)

    async def _deal_damage_to_target(self, guid, amount, school, spell_id,
                                     caster_guid):
        self.damage.append((int(guid), int(amount)))

    async def _damage_player(self, amount, source, physical=False):
        self.player_damage.append(int(amount))

    async def _heal_player(self, amount):
        return int(amount), 0

    async def _heal_creature(self, *args, **kwargs):
        return None

    async def _send_spell_damage_log(self, *args, **kwargs):
        return None

    async def _send_spell_heal_log(self, *args, **kwargs):
        return None

    async def _send_attacker_state_update(self, *args, **kwargs):
        return None

    async def _send_spell_go(self, *args, **kwargs):
        return None

    async def send_packet(self, *args, **kwargs):
        return None


def _cast(session, template, target=0, dest=None):
    cast = spells.SpellCast(session.player_guid, template.id, target)
    cast.template = template
    if dest is not None:
        cast.x, cast.y, cast.z = dest
        cast.has_dest = True
    return cast


@_async_test
async def test_self_centred_area_damage():
    session = FakeSession()
    spell = spells.get_spell_template(WORLD_DB, 1449)   # Arcane Explosion
    assert spell.effect_area_kind(0) == "enemy"
    near = session.add_creature(1, 5.0)
    session.add_creature(2, 12.0)
    await session._apply_spell_effects(_cast(session, spell), spell, 0)
    assert [guid for guid, _ in session.damage] == [near]


@_async_test
async def test_destination_area_and_ground_ticks():
    session = FakeSession()
    spell = spells.get_spell_template(WORLD_DB, 2120)   # Flamestrike
    target = session.add_creature(3, 9.0)
    session.add_creature(4, 30.0)
    await session._apply_spell_effects(
        _cast(session, spell, dest=(10.0, 0.0, 0.0)), spell, 0)
    assert [guid for guid, _ in session.damage] == [target]
    assert len(session._combat_state["area_effects"]) == 1
    ground = session._combat_state["area_effects"][0]
    assert ground["amount"] == 12 and ground["interval"] == 2.0
    session.damage.clear()
    await session._tick_area_effects(ground["next_tick"] + 0.01)
    assert [guid for guid, _ in session.damage] == [target]
    await session._tick_area_effects(ground["expires"] + 1.0)
    assert session._combat_state["area_effects"] == []


@_async_test
async def test_cone_only_hits_in_front():
    session = FakeSession()
    spell = spells.get_spell_template(WORLD_DB, 120)    # Cone of Cold
    assert spell.effect_area_kind(1) == "cone"
    front = session.add_creature(5, 5.0, 0.0)
    session.add_creature(6, -5.0, 0.0)
    await session._apply_spell_effects(_cast(session, spell), spell, 0)
    assert [guid for guid, _ in session.damage] == [front]


@_async_test
async def test_chain_lightning_jumps_and_falls_off():
    session = FakeSession()
    spell = spells.get_spell_template(WORLD_DB, 421)    # Chain Lightning
    first = session.add_creature(7, 5.0)
    second = session.add_creature(8, 10.0)
    third = session.add_creature(9, 15.0)
    session.add_creature(10, 40.0)
    await session._apply_spell_effects(_cast(session, spell, first), spell,
                                       first)
    assert [guid for guid, _ in session.damage] == [first, second, third]
    amounts = [amount for _, amount in session.damage]
    assert amounts[0] > amounts[1] > amounts[2]


@_async_test
async def test_max_targets_and_triggered_offhand():
    session = FakeSession()
    spell = spells.get_spell_template(WORLD_DB, 1680)   # Whirlwind
    assert spell.max_targets == 4
    for low in range(20, 26):
        session.add_creature(low, 1.0 + low * 0.1)
    await session._apply_spell_effects(_cast(session, spell), spell, 0)
    assert len({guid for guid, _ in session.damage}) == 4


@_async_test
async def test_cast_packet_destination_is_parsed():
    session = FakeSession()
    session.known_spell_ids = {2120}          # Flamestrike
    session._send_spell_go = _noop
    session.send_packet = _noop
    target = session.add_creature(40, 9.0)
    payload = (
        struct.pack("<BiB", 1, 2120, 0)
        + struct.pack("<I", 0x40)             # TARGET_FLAG_DEST_LOCATION
        + b"\x00"                             # empty transport guid
        + struct.pack("<fff", 10.0, 0.0, 0.0)
    )
    await session.handle_cast_spell(payload)
    pending = session._combat_state["pending_cast"]
    assert pending is not None
    assert pending["dest"] == (10.0, 0.0, 0.0)
    session._combat_state["pending_cast"] = None
    await session._apply_pending_cast(pending)
    assert [guid for guid, _ in session.damage] == [target]
    assert len(session._combat_state["area_effects"]) == 1


async def _noop(*args, **kwargs):
    return None


@_async_test
async def test_creature_area_and_hostile_ground_effect():
    session = FakeSession()
    spell = spells.get_spell_template(WORLD_DB, 20296)  # Flamestrike (NPC)
    low = 30
    session.add_creature(low, 5.0, 5.0)
    spawn = session._spawns[low]
    spawn.update({"name": "Caster", "crit_chance": 0.0, "o": 0.0})
    ai = {"entry": int(spawn["entry"]), "level": 20}
    await CreatureAIMixin._apply_creature_spell_effects(
        session, low, spawn, ai, spell, session.player_guid)
    assert len(session.player_damage) == 1
    ground = session._combat_state["area_effects"][0]
    assert ground["hostile_to_player"] is True
    session.player_damage.clear()
    await session._tick_area_effects(ground["next_tick"] + 0.01)
    assert len(session.player_damage) == 1
