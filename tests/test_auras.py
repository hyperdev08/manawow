"""Aura components: slows, roots, stuns, stats and periodic durations."""
from __future__ import annotations

import asyncio
import os
import struct
import sys
import time
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src.protocol.opcodes import Opcodes
from src.world import creature, spells
from src.world.realmsocket import WorldSocket
from src.world.worldsocket.auras import AurasMixin
from src.world.worldsocket.creature_ai import CreatureAIMixin
from src.world.worldsocket.spells import SpellsMixin
from src.world.worldsocket.stats import StatsMixin

WORLD_DB = os.path.join(ROOT, "data", "world.db")


def _async_test(fn):
    """Run an async test body without pytest-asyncio."""
    def wrapper(*args, **kwargs):
        return asyncio.run(fn(*args, **kwargs))
    wrapper.__name__ = fn.__name__
    return wrapper


class FakeAura(AurasMixin, SpellsMixin, StatsMixin):
    def __init__(self):
        self.config = types.SimpleNamespace(
            database=types.SimpleNamespace(world_path=WORLD_DB))
        self.character = {
            "guid": 999999, "name": "Aura", "class": 8, "race": 1,
            "level": 20, "x": 0.0, "y": 0.0, "z": 0.0, "o": 0.0,
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
        self._next_move = 1
        self.sent = []
        self.field_updates = []
        self._combat_state = {
            "dead": False, "dots": [], "area_effects": [],
            "auras": [], "creature_auras": {}, "aura_state": {},
            "aggro": {}, "idle": {}, "idle_settle": {},
        }

    def add_creature(self, low, x=0.0, y=0.0, z=0.0):
        self._spawns[low] = {
            "guid": low, "entry": 1000 + low, "x": x, "y": y, "z": z,
            "o": 0.0, "dead_since": 0, "combat_reach": 1.5,
            "min_level": 20, "base_health": 500, "hp_multiplier": 1.0,
            "unit_flags": 0,
        }
        return creature.creature_guid(low, 1000 + low)

    def _get_combat_state(self):
        return self._combat_state

    def _is_player_guid(self, guid):
        return int(guid) == self.player_guid

    def _player_position(self):
        return (float(self.character["x"]), float(self.character["y"]),
                float(self.character["z"]))

    def _distance(self, a, b):
        return WorldSocket._distance(a, b)

    def _creature_damage(self, spawn):
        return WorldSocket._creature_damage(self, spawn)

    def _get_creature_spawn(self, low):
        return self._spawns.get(int(low))

    def _creature_position(self, low):
        spawn = self._spawns[int(low)]
        return (float(spawn["x"]), float(spawn["y"]), float(spawn["z"]))

    def _creature_object_guid(self, low, entry=0):
        if not entry:
            entry = int(self._spawns[int(low)]["entry"])
        return creature.creature_guid(int(low), int(entry))

    def _next_move_id(self):
        self._next_move += 1
        return self._next_move

    async def send_packet(self, opcode, payload=b""):
        self.sent.append((int(opcode), bytes(payload)))

    async def _send_player_fields(self, fields):
        self.field_updates.append(dict(fields))

    async def _set_player_power(self, power):
        self.character["power"] = int(power)

    async def _send_spell_heal_log(self, *args, **kwargs):
        return None

    async def _send_spell_damage_log(self, *args, **kwargs):
        return None

    async def _send_creature_fields(self, *args, **kwargs):
        return None

    async def _deal_damage_to_target(self, *args, **kwargs):
        return None

    async def _damage_player(self, amount, source, physical=False):
        self.player_damage = getattr(self, "player_damage", [])
        self.player_damage.append(int(amount))

    async def _heal_creature(self, *args, **kwargs):
        return None


async def _apply(fake, spell_id, effect_index, target_kind, target_guid,
                 caster_guid=None):
    template = spells.get_spell_template(WORLD_DB, spell_id)
    effect = template.effects[effect_index]
    return await fake._apply_aura_from_effect(
        template, effect, effect_index,
        int(fake.player_guid if caster_guid is None else caster_guid),
        target_guid, int(fake.character["level"]),
        target_kind=target_kind)


@_async_test
async def test_creature_slow_root_and_stun():
    fake = FakeAura()
    low = 1
    guid = fake.add_creature(low)

    # Frostbolt chill: -40% movement, 5 s.
    await _apply(fake, 116, 0, "creature", guid)
    mods = fake._creature_aura_mods(low)
    assert abs(mods["speed_mult"] - 0.6) < 0.001
    assert fake._creature_can_move(low)
    await fake._tick_auras(time.monotonic() + 6.0)
    assert fake._creature_aura_mods(low)["speed_mult"] == 1.0

    # Frost Nova: 8 s root, no movement.
    await _apply(fake, 122, 1, "creature", guid)
    assert not fake._creature_can_move(low)
    assert any(opcode == int(Opcodes.SMSG_MONSTER_MOVE)
               for opcode, _ in fake.sent)
    await fake._tick_auras(time.monotonic() + 9.0)
    assert fake._creature_can_move(low)

    # Hammer of Justice: 3 s stun, cannot attack or cast.
    await _apply(fake, 853, 0, "creature", guid)
    assert not fake._creature_can_move(low)
    assert not fake._creature_can_attack(low)
    assert not fake._creature_can_cast(low)
    await fake._tick_auras(time.monotonic() + 4.0)
    assert fake._creature_can_attack(low)


@_async_test
async def test_player_stat_and_attack_power_auras():
    fake = FakeAura()
    fake._apply_combat_stats()
    base_inte = int(fake.character["stats"]["inte"])
    base_power = int(fake.character["max_power"])
    # Arcane Intellect: +2 Intellect for 30 min.
    await _apply(fake, 1459, 0, "player", fake.player_guid)
    assert int(fake.character["stats"]["inte"]) == base_inte + 2
    assert int(fake.character["max_power"]) >= base_power

    # Battle Shout: 15 AP + 0.5 per level up to level 11 -> 20 at level 20.
    await _apply(fake, 6673, 0, "player", fake.player_guid)
    mods = fake._player_aura_mods()
    assert mods["attack_power"] == 20
    assert mods["ranged_attack_power"] == 20
    assert int(fake.character["attack_power"]) >= 15

    # Expire everything.
    await fake._tick_auras(time.monotonic() + 2000.0)
    assert fake._player_aura_mods()["attack_power"] == 0
    assert int(fake.character["stats"]["inte"]) == base_inte


@_async_test
async def test_player_speed_root_and_stun_packets():
    fake = FakeAura()
    # Sprint: +50% -> 10.5 yd/s.
    await _apply(fake, 2983, 0, "player", fake.player_guid)
    speed_packets = [payload for opcode, payload in fake.sent
                     if opcode == int(Opcodes.SMSG_FORCE_RUN_SPEED_CHANGE)]
    assert speed_packets
    assert abs(struct.unpack_from("<f", speed_packets[-1], 6)[0] - 10.5) < 0.01
    await fake._tick_auras(time.monotonic() + 16.0)
    assert fake._player_aura_mods()["speed_mult"] == 1.0

    # Stun (Hammer of Justice) roots and sets UNIT_FLAG_STUNNED.
    await _apply(fake, 853, 0, "player", fake.player_guid)
    assert any(opcode == int(Opcodes.SMSG_FORCE_MOVE_ROOT)
               for opcode, _ in fake.sent)
    assert any(fields.get(59, 0) & 0x00040000
               for fields in fake.field_updates)
    await fake._tick_auras(time.monotonic() + 4.0)
    assert any(opcode == int(Opcodes.SMSG_FORCE_MOVE_UNROOT)
               for opcode, _ in fake.sent)


@_async_test
async def test_periodic_aura_uses_spell_duration():
    fake = FakeAura()
    low = 5
    guid = fake.add_creature(low)
    template = spells.get_spell_template(WORLD_DB, 133)      # Fireball
    effect = template.effects[1]                              # 4 s DoT
    fake._apply_periodic_aura(template, effect, 1, 20, guid)
    dot = fake._combat_state["dots"][0]
    duration = dot["expires"] - (dot["next_tick"] - dot["interval"])
    assert abs(duration - 4.0) < 0.01, duration


@_async_test
async def test_frost_nova_roots_through_effect_loop():
    fake = FakeAura()
    near_low = 10
    far_low = 11
    fake.add_creature(near_low, 4.0)
    fake.add_creature(far_low, 30.0)
    template = spells.get_spell_template(WORLD_DB, 122)       # Frost Nova
    cast = spells.SpellCast(fake.player_guid, template.id, 0)
    cast.template = template
    await fake._apply_spell_effects(cast, template, 0)
    assert fake._creature_aura_mods(near_low)["rooted"]
    assert not fake._creature_aura_mods(far_low)["rooted"]


@_async_test
async def test_stun_blocks_player_cast():
    fake = FakeAura()
    fake._apply_combat_stats()
    fake.known_spell_ids = {133}
    await _apply(fake, 853, 0, "player", fake.player_guid)
    fake.sent.clear()
    await fake.handle_cast_spell(struct.pack("<BiBI", 1, 133, 0, 0))
    failed = [(opcode, payload) for opcode, payload in fake.sent
              if opcode == int(Opcodes.SMSG_CAST_FAILED)]
    assert failed
    assert failed[0][1][5] == 22          # SPELL_FAILED_CASTER_AURASTATE


@_async_test
async def test_creature_casts_slow_on_player():
    fake = FakeAura()
    low = 20
    fake.add_creature(low, 5.0, 0.0)
    spawn = fake._spawns[low]
    spawn.update({"name": "Caster", "crit_chance": 0.0, "o": 0.0})
    template = spells.get_spell_template(WORLD_DB, 116)       # Frostbolt
    ai = {"entry": int(spawn["entry"]), "level": 20}
    await CreatureAIMixin._apply_creature_spell_effects(
        fake, low, spawn, ai, template, fake.player_guid)
    assert abs(fake._player_aura_mods()["speed_mult"] - 0.6) < 0.001
    speeds = [payload for opcode, payload in fake.sent
              if opcode == int(Opcodes.SMSG_FORCE_RUN_SPEED_CHANGE)]
    assert speeds
    assert abs(struct.unpack_from("<f", speeds[-1], 6)[0] - 4.2) < 0.01
