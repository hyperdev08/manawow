"""Attack resolution, spell mitigation, dispels, channels and CC."""
from __future__ import annotations

import asyncio
import math
import os
import sqlite3
import struct
import sys
import tempfile
import time
import types
from pathlib import Path
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src.protocol.opcodes import Opcodes
from src.world import creature, databases, spells
from src.world import los
from src.world.realmsocket import WorldSocket
from src.world.worldsocket.auras import AurasMixin
from src.world.worldsocket.combat import CombatMixin
from src.world.worldsocket.creature_ai import CreatureAIMixin
from src.world.worldsocket.creatures import CreaturesMixin
from src.world.worldsocket.spells import SpellsMixin
from src.world.worldsocket.stats import StatsMixin

WORLD_DB = os.path.join(ROOT, "data", "world.db")


def _async_test(fn):
    def wrapper(*args, **kwargs):
        return asyncio.run(fn(*args, **kwargs))
    wrapper.__name__ = fn.__name__
    return wrapper


class FakeUnit(AurasMixin, SpellsMixin, StatsMixin, CombatMixin,
               CreaturesMixin):
    """A tiny in-memory player + creature world for deterministic tests."""

    def __init__(self, char_class=8, level=20):
        self.config = types.SimpleNamespace(
            database=types.SimpleNamespace(world_path=WORLD_DB))
        self.character = {
            "guid": 999999, "name": "Tester", "class": char_class,
            "race": 1, "level": level, "x": 0.0, "y": 0.0, "z": 0.0,
            "o": 0.0, "health": 1000, "max_health": 1000, "power": 1000,
            "max_power": 1000, "map": 0,
        }
        self.player_guid = 0x42
        self.in_world = True
        self.log = types.SimpleNamespace(
            debug=lambda *a, **k: None, info=lambda *a, **k: None,
            exception=lambda *a, **k: None, warning=lambda *a, **k: None)
        self._summoned_creatures = {}
        self._spawns = {}
        self._creature_spawn_cache = self._spawns
        self._visible_creatures = {}
        self._next_move = 1
        self.sent = []
        self.field_updates = []
        self._combat_state = {
            "dead": False, "dots": [], "area_effects": [],
            "auras": [], "creature_auras": {}, "aura_state": {},
            "aggro": {}, "idle": {}, "idle_settle": {},
            "pending_cast": None, "channel": None, "school_lockout": {},
            "cooldowns": {}, "gcd_until": 0.0, "in_combat": False,
            "target_guid": 0, "auto_attacking": False,
            "ranged_attacking": False, "last_swing": 0.0,
            "last_ranged_swing": 0.0, "swing_interval": 2.0,
            "ranged_interval": 2.0, "last_respawn_scan": 0.0,
            "selected_guid": 0, "loot_target": 0, "loot_items": [],
            "loot_taken": [], "loot_money": 0,
            "combo_target": 0, "combo_points": 0, "runes": [],
            "last_rune_regen": 0.0, "rune_prediction": None,
            "last_power_use": 0.0,
        }
        self._apply_combat_stats()

    # ---- world / positions -------------------------------------------------
    def add_creature(self, low, x=0.0, y=0.0, z=0.0, level=20, kind=7):
        self._spawns[low] = {
            "guid": low, "entry": 1000 + low, "x": x, "y": y, "z": z,
            "o": 0.0, "dead_since": 0, "combat_reach": 1.5,
            "min_level": level, "max_level": level, "base_health": 500,
            "hp_multiplier": 1.0, "type": kind, "faction": 14,
            "current_health": 500, "current_power": 0, "armor": 0,
            "min_damage": 0.0, "max_damage": 0.0, "attack_power": 0,
            "melee_attack_speed": 2000,
            "unit_flags": 0, "name": "Target",
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

    def _creature_position(self, low):
        spawn = self._spawns[int(low)]
        return (float(spawn["x"]), float(spawn["y"]), float(spawn["z"]))

    def _creature_object_guid(self, low, entry=0):
        if not entry:
            entry = int(self._spawns[int(low)]["entry"])
        return creature.creature_guid(int(low), int(entry))

    def _get_creature_spawn(self, low):
        return self._spawns.get(int(low))

    def _creature_damage(self, spawn):
        return WorldSocket._creature_damage(self, spawn)

    def _next_move_id(self):
        self._next_move += 1
        return self._next_move

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
            guid |= data[pos] << (index * 8)
            pos += 1
        return guid, pos

    def _server_tick_ms(self):
        return int(time.monotonic() * 1000) & 0xFFFFFFFF

    def _in_front(self, observer, facing, target, arc=math.pi):
        return WorldSocket._in_front(self, observer, facing, target, arc)

    def _target_in_front(self, target_guid, arc=math.pi):
        return WorldSocket._target_in_front(self, target_guid, arc)

    async def _has_line_of_sight(self, start, end, map_id=None):
        return await WorldSocket._has_line_of_sight(self, start, end, map_id)

    def _unit_eye_position(self, guid):
        return WorldSocket._unit_eye_position(self, guid)

    # ---- health / damage ---------------------------------------------------
    def _creature_health(self, guid):
        low = creature.creature_low_guid(guid)
        spawn = self._spawns.get(low)
        if spawn is None or int(spawn["dead_since"] or 0) > 0:
            return 0
        return int(spawn["current_health"])

    def _creature_max_health(self, spawn):
        return max(1, int(spawn["base_health"]))

    def _set_creature_health(self, low, health):
        self._spawns[int(low)]["current_health"] = max(0, int(health))

    async def _deal_damage_to_creature(self, guid, damage, attacker_guid=0,
                                       threat_kind="melee"):
        low = creature.creature_low_guid(guid)
        spawn = self._spawns.get(low)
        if spawn is None or int(spawn["dead_since"] or 0) > 0:
            return False
        new = max(0, int(spawn["current_health"]) - max(1, int(damage)))
        spawn["current_health"] = new
        self._aggro_creature(low)
        self._add_threat(low, int(attacker_guid or self.player_guid),
                         max(1.0, float(damage)), kind=threat_kind)
        if new <= 0:
            spawn["dead_since"] = int(time.time())
            return True
        return False

    def _victim_health(self, guid):
        if self._is_player_guid(guid):
            return int(self.character.get("health", 0))
        return self._creature_health(guid)

    async def _damage_player(self, damage, source, physical=False):
        amount = max(1, int(damage))
        self.character["health"] = max(0, int(self.character["health"]) - amount)

    async def _heal_player(self, amount, healer_guid=None):
        current = int(self.character["health"])
        maximum = int(self.character["max_health"])
        healed = min(maximum - current, max(0, int(amount)))
        self.character["health"] = current + healed
        return healed, int(amount) - healed

    async def _heal_creature(self, low, spawn, amount, template=None,
                             caster_guid=0):
        self._set_creature_health(
            low, min(self._creature_max_health(spawn),
                     self._creature_health(low) + int(amount)))

    # ---- packets -----------------------------------------------------------
    async def send_packet(self, opcode, payload=b""):
        self.sent.append((int(opcode), bytes(payload)))

    async def _send_player_fields(self, fields):
        self.field_updates.append(dict(fields))

    async def _send_creature_fields(self, low, fields):
        self.field_updates.append(dict(fields))

    async def _set_player_power(self, power):
        self.character["power"] = int(power)

    async def _send_spell_heal_log(self, *args, **kwargs):
        self.sent.append((int(Opcodes.SMSG_SPELLHEALLOG), b"heal"))

    async def _send_spell_damage_log(self, *args, **kwargs):
        self.sent.append((int(Opcodes.SMSG_SPELLNONMELEEDAMAGELOG), b"damage"))

    def _combo_target(self):
        return int(self._combat_state.get("combo_target") or 0)

    def _combo_points(self):
        return int(self._combat_state.get("combo_points") or 0)

    def _is_death_knight(self):
        return False

    def _note_rune_prediction(self, template):
        return None

    def _create_mana(self):
        return 100

    def _power_type(self):
        return 0

    def _max_power(self):
        return int(self.character["max_power"])


def _cast(session, template, target=0, dest=None):
    cast = spells.SpellCast(session.player_guid, template.id, target)
    cast.template = template
    if dest is not None:
        cast.x, cast.y, cast.z = dest
        cast.has_dest = True
    return cast


def _apply_aura(session, spell_id, effect_index, target_kind, target_guid):
    template = spells.get_spell_template(WORLD_DB, spell_id)
    return session._apply_aura_from_effect(
        template, template.effects[effect_index], effect_index,
        session.player_guid, target_guid,
        int(session.character["level"]), target_kind=target_kind)


@_async_test
async def test_spell_miss_immune_and_absorb():
    session = FakeUnit()
    target = session.add_creature(1, 5.0)
    fireball = spells.get_spell_template(WORLD_DB, 133)
    cast = _cast(session, fireball, target)
    before = session._creature_health(target)

    # 5% hit chance with a forced 1.0 roll -> miss, no damage.
    session.character["spell_hit_chance"] = 5.0
    with mock.patch("random.uniform", return_value=1.0):
        await session._apply_spell_effects(cast, fireball, target)
    assert session._creature_health(target) == before
    assert any(op == int(Opcodes.SMSG_SPELLLOGMISS)
               for op, _ in session.sent)

    # Divine Shield/scool immunity makes the next cast immune.
    await session._apply_aura_from_effect(
        spells.get_spell_template(WORLD_DB, 642),
        spells.get_spell_template(WORLD_DB, 642).effects[0], 0,
        session.player_guid, target, 40, target_kind="creature")
    session.sent.clear()
    session.character["spell_hit_chance"] = 100.0
    await session._apply_spell_effects(cast, fireball, target)
    assert session._creature_health(target) == before
    assert any(op == int(Opcodes.SMSG_SPELLLOGMISS)
               for op, _ in session.sent)

    # Power Word: Shield absorbs before health changes.
    session.character["health"] = session._max_health()
    health_before = session.character["health"]
    shield = spells.get_spell_template(WORLD_DB, 17)
    await session._apply_aura_from_effect(
        shield, shield.effects[0], 0, session.player_guid,
        session.player_guid, 20, target_kind="player")
    session.character["health"] = health_before
    await session._deal_spell_damage(
        session.player_guid, 20, spells.SCHOOL_HOLY, shield,
        session.player_guid)
    assert session.character["health"] == health_before


@_async_test
async def test_dispel_removes_debuff_and_sends_log():
    session = FakeUnit()
    frostbolt = spells.get_spell_template(WORLD_DB, 116)
    await session._apply_aura_from_effect(
        frostbolt, frostbolt.effects[0], 0, session.player_guid,
        session.player_guid, 20, target_kind="player")
    assert any(aura["spell_id"] == 116 for aura in session._combat_state["auras"])
    session.sent.clear()
    await session._dispel_target(
        session.player_guid, spells.DISPEL_MAGIC, 1, offensive=False)
    assert not any(aura["spell_id"] == 116
                   for aura in session._combat_state["auras"])
    assert any(op == int(Opcodes.SMSG_SPELLDISPELLOG)
               for op, _ in session.sent)


@_async_test
async def test_aura_packet_has_slot_spell_and_duration():
    session = FakeUnit()
    arcane = spells.get_spell_template(WORLD_DB, 1459)
    await session._apply_aura_from_effect(
        arcane, arcane.effects[0], 0, session.player_guid,
        session.player_guid, 20, target_kind="player")
    packets = [payload for op, payload in session.sent
               if op == int(Opcodes.SMSG_AURA_UPDATE)]
    assert packets
    # Packed guid (mask + 0x42) then slot, spell id, flags...
    assert struct.unpack_from("<I", packets[-1], 3)[0] == 1459


@_async_test
async def test_channel_ticks_trigger_spell():
    session = FakeUnit()
    target = session.add_creature(2, 10.0)
    missiles = spells.get_spell_template(WORLD_DB, 5143)
    assert missiles.is_channeled()
    cast = _cast(session, missiles, target)
    before = session._creature_health(target)
    session.character["spell_hit_chance"] = 100.0
    await session._start_channel(cast, missiles, target, 1)
    channel = session._combat_state["channel"]
    assert channel["trigger_spell"] == 7268
    await session._tick_channel(float(channel["next_tick"]) + 0.01)
    assert session._creature_health(target) < before


@_async_test
async def test_spell_interrupt_clears_cast_and_locks_school():
    session = FakeUnit()
    target = session.add_creature(3, 5.0)
    session._combat_state["aggro"][3] = {
        "ai": {"entry": 1003, "cast": {
            "cast_id": 1, "spell_id": 116, "target_guid": session.player_guid,
            "template": spells.get_spell_template(WORLD_DB, 116),
            "finish_at": time.monotonic() + 2.0, "cost": 0}},
    }
    await session._interrupt_target_cast(target, spells.SCHOOL_FROST)
    ai = session._combat_state["aggro"][3]["ai"]
    assert ai["cast"] is None
    assert ai["school_lockout"]["school"] == spells.SCHOOL_FROST
    assert any(op == int(Opcodes.SMSG_SPELL_FAILURE)
               for op, _ in session.sent)


@_async_test
async def test_diminishing_returns_reduce_stun_duration():
    session = FakeUnit()
    target = session.add_creature(4, 5.0)
    hammer = spells.get_spell_template(WORLD_DB, 853)
    durations = []
    for _ in range(4):
        before = time.monotonic()
        applied = await session._apply_aura_from_effect(
            hammer, hammer.effects[0], 0, session.player_guid, target, 20,
            target_kind="creature")
        if applied:
            entry = session._aura_store("creature", target)[0]
            durations.append(round(entry["expires"] - before, 2))
        else:
            durations.append(0.0)
        # reset between applications would defeat DR; the 4th is immune.
    assert abs(durations[0] - 3.0) < 0.05
    assert durations[1] < durations[0]
    assert durations[2] < durations[1]
    assert durations[3] == 0.0


@_async_test
async def test_fear_blocks_cast_and_creature_control():
    session = FakeUnit()
    target = session.add_creature(5, 5.0)
    fear = spells.get_spell_template(WORLD_DB, 5782)
    await session._apply_aura_from_effect(
        fear, fear.effects[0], 0, session.player_guid, session.player_guid,
        20, target_kind="player")
    assert not session._player_can_cast(133)
    assert session._player_aura_mods()["feared"]
    await session._apply_aura_from_effect(
        fear, fear.effects[0], 0, session.player_guid, target, 20,
        target_kind="creature")
    assert not session._creature_can_attack(5)
    assert not session._creature_can_cast(5)


@_async_test
async def test_aura_slots_stay_stable_after_expiry():
    session = FakeUnit()
    arcane = spells.get_spell_template(WORLD_DB, 1459)
    shout = spells.get_spell_template(WORLD_DB, 6673)
    await session._apply_aura_from_effect(
        arcane, arcane.effects[0], 0, session.player_guid,
        session.player_guid, 20, target_kind="player")
    await session._apply_aura_from_effect(
        shout, shout.effects[0], 0, session.player_guid,
        session.player_guid, 20, target_kind="player")
    slots = {aura["spell_id"]: aura["slot"]
             for aura in session._combat_state["auras"]}
    assert slots[1459] != slots[6673]
    session.sent.clear()
    await session._tick_auras(time.monotonic() + 200.0)
    remaining = {aura["spell_id"]: aura["slot"]
                 for aura in session._combat_state["auras"]}
    assert remaining[1459] == slots[1459]
    # The removal names the expired aura's original slot, not the survivor's.
    removal = [payload for op, payload in session.sent
               if op == int(Opcodes.SMSG_AURA_UPDATE)
               and payload[2] == slots[6673]
               and struct.unpack_from("<I", payload, 3)[0] == 0]
    assert removal


@_async_test
async def test_creature_melee_damage_is_not_guid_sized():
    session = FakeUnit()
    session.add_creature(11, 5.0, level=1)
    spawn = session._spawns[11]
    # A level-1 wolf has no template damage, so the fallback is ~1-2.
    for _ in range(20):
        damage = session._creature_damage(spawn)
        assert 0.5 <= damage <= 5.0, damage


@_async_test
async def test_self_targeted_cast_does_not_confuse_peer_detection():
    session = FakeUnit()
    session.known_spell_ids = {1459}
    payload = (struct.pack("<BiBI", 1, 1459, 0, 0x02)
               + spells.pack_guid(session.player_guid))
    await session.handle_cast_spell(payload)
    assert any(aura["spell_id"] == 1459
               for aura in session._combat_state["auras"])


@_async_test
async def test_line_of_sight_geometry_and_optional_db_blockers():
    box = los.SightBlocker(0, 5.0, 7.0, -1.0, 1.0, 0.0, 3.0)
    assert not los.has_line_of_sight(
        (0.0, 0.0, 1.0), (10.0, 0.0, 1.0), [box])
    assert los.has_line_of_sight(
        (0.0, 5.0, 1.0), (10.0, 5.0, 1.0), [box])
    # A unit standing inside a doorway is not blinded by its own box.
    assert los.has_line_of_sight(
        (6.0, 0.0, 1.0), (10.0, 0.0, 1.0), [box])

    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, "los.db")
        con = sqlite3.connect(path)
        con.execute(
            "CREATE TABLE sight_blockers(map INTEGER, min_x REAL, max_x REAL,"
            " min_y REAL, max_y REAL, min_z REAL, max_z REAL)")
        con.execute("INSERT INTO sight_blockers VALUES(0, 5, 7, -1, 1, 0, 3)")
        con.execute("INSERT INTO sight_blockers VALUES(1, 5, 7, -1, 1, 0, 3)")
        con.commit()
        con.close()
        los.clear_cache()
        assert not los.has_los(
            path, 0, (0.0, 0.0, 1.0), (10.0, 0.0, 1.0))
        assert los.has_los(
            path, 2, (0.0, 0.0, 1.0), (10.0, 0.0, 1.0))
        session = FakeUnit()
        session.config.database.world_path = path
        session.character["map"] = 0
        assert not await session._has_line_of_sight(
            (0.0, 0.0, 1.0), (10.0, 0.0, 1.0))
        session.character["map"] = 2
        assert await session._has_line_of_sight(
            (0.0, 0.0, 1.0), (10.0, 0.0, 1.0))


@_async_test
async def test_melee_and_spell_facing_checks():
    session = FakeUnit()
    target = session.add_creature(20, -5.0)
    session.character["o"] = 0.0
    await session.handle_attack_swing(struct.pack("<Q", target))
    assert any(op == int(Opcodes.SMSG_ATTACKSWING_BADFACING)
               for op, _ in session.sent)
    session.sent.clear()
    front = session.add_creature(21, 5.0)
    await session.handle_attack_swing(struct.pack("<Q", front))
    assert any(op == int(Opcodes.SMSG_ATTACKSTART)
               for op, _ in session.sent)

    # A spell with FacingCasterFlags=1 refuses a target behind the caster.
    session.known_spell_ids = {133}
    fireball = spells.get_spell_template(WORLD_DB, 133)
    fireball.facing_caster_flags = 1
    original_get = spells.get_spell_template
    with mock.patch("src.world.spells.get_spell_template",
                    side_effect=lambda path, sid: fireball
                    if int(sid) == 133 else original_get(path, sid)):
        payload = (struct.pack("<BiBI", 1, 133, 0, 0x02)
                   + spells.pack_guid(target))
        await session.handle_cast_spell(payload)
    failed = [p for op, p in session.sent
              if op == int(Opcodes.SMSG_CAST_FAILED)]
    assert failed and failed[-1][5] == 61      # SPELL_FAILED_NOT_INFRONT


@_async_test
async def test_weapon_stats_and_procs_are_cached_for_the_session():
    uri = "file:weapon-cache?mode=memory&cache=shared"
    keep = sqlite3.connect(uri, uri=True)
    try:
        keep.execute(
            "CREATE TABLE item_templates(entry INTEGER PRIMARY KEY,"
            " damage_min1 REAL, damage_max1 REAL, delay INTEGER,"
            " spell_id1 INTEGER DEFAULT 0, spell_trigger1 INTEGER DEFAULT 0,"
            " spell_ppm_rate1 REAL DEFAULT 0, spell_cooldown1 INTEGER DEFAULT 0,"
            " spell_charges1 INTEGER DEFAULT 0,"
            " spell_id2 INTEGER DEFAULT 0, spell_trigger2 INTEGER DEFAULT 0,"
            " spell_ppm_rate2 REAL DEFAULT 0, spell_cooldown2 INTEGER DEFAULT 0,"
            " spell_charges2 INTEGER DEFAULT 0,"
            " spell_id3 INTEGER DEFAULT 0, spell_trigger3 INTEGER DEFAULT 0,"
            " spell_ppm_rate3 REAL DEFAULT 0, spell_cooldown3 INTEGER DEFAULT 0,"
            " spell_charges3 INTEGER DEFAULT 0,"
            " spell_id4 INTEGER DEFAULT 0, spell_trigger4 INTEGER DEFAULT 0,"
            " spell_ppm_rate4 REAL DEFAULT 0, spell_cooldown4 INTEGER DEFAULT 0,"
            " spell_charges4 INTEGER DEFAULT 0,"
            " spell_id5 INTEGER DEFAULT 0, spell_trigger5 INTEGER DEFAULT 0,"
            " spell_ppm_rate5 REAL DEFAULT 0, spell_cooldown5 INTEGER DEFAULT 0,"
            " spell_charges5 INTEGER DEFAULT 0)")
        keep.execute(
            "CREATE TABLE character_items(id INTEGER PRIMARY KEY,"
            " character_guid INTEGER, item_entry INTEGER, slot INTEGER)")
        keep.execute(
            "INSERT INTO item_templates(entry, damage_min1, damage_max1,"
            " delay, spell_id1, spell_trigger1, spell_ppm_rate1,"
            " spell_cooldown1, spell_charges1)"
            " VALUES(100, 5.0, 9.0, 2000, 17152, 2, 1.0, 0, 0)")
        keep.execute("INSERT INTO character_items VALUES(1, 42, 100, 15)")
        keep.commit()

        session = FakeUnit()
        session.config.database.world_path = "weapon-cache"
        session.player_guid = 42
        with mock.patch(
                "src.world.worldsocket.combat.databases.connect_character",
                side_effect=lambda _path: sqlite3.connect(uri, uri=True)) as connect:
            first = session._player_weapon_stats()
            second = session._player_weapon_stats()
            procs = session._equipped_item_proc_entries(15)
            procs_again = session._equipped_item_proc_entries(15)
        assert first == second and first["min"] == 5.0
        assert (second["min"], second["max"]) == (5.0, 9.0)
        assert procs is procs_again and procs[0]["spell_id"] == 17152
        assert connect.call_count == 2       # one weapon row, one proc row
    finally:
        keep.close()


@_async_test
async def test_item_ppm_proc_rolls_on_hit():
    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, "proc.db")
        con = sqlite3.connect(path)
        con.execute(
            "CREATE TABLE item_templates(entry INTEGER PRIMARY KEY,"
            " spell_id1 INTEGER DEFAULT 0, spell_trigger1 INTEGER DEFAULT 0,"
            " spell_ppm_rate1 REAL DEFAULT 0, spell_cooldown1 INTEGER DEFAULT 0,"
            " spell_charges1 INTEGER DEFAULT 0,"
            " spell_id2 INTEGER DEFAULT 0, spell_trigger2 INTEGER DEFAULT 0,"
            " spell_ppm_rate2 REAL DEFAULT 0, spell_cooldown2 INTEGER DEFAULT 0,"
            " spell_charges2 INTEGER DEFAULT 0,"
            " spell_id3 INTEGER DEFAULT 0, spell_trigger3 INTEGER DEFAULT 0,"
            " spell_ppm_rate3 REAL DEFAULT 0, spell_cooldown3 INTEGER DEFAULT 0,"
            " spell_charges3 INTEGER DEFAULT 0,"
            " spell_id4 INTEGER DEFAULT 0, spell_trigger4 INTEGER DEFAULT 0,"
            " spell_ppm_rate4 REAL DEFAULT 0, spell_cooldown4 INTEGER DEFAULT 0,"
            " spell_charges4 INTEGER DEFAULT 0,"
            " spell_id5 INTEGER DEFAULT 0, spell_trigger5 INTEGER DEFAULT 0,"
            " spell_ppm_rate5 REAL DEFAULT 0, spell_cooldown5 INTEGER DEFAULT 0,"
            " spell_charges5 INTEGER DEFAULT 0)")
        con.execute(
            "CREATE TABLE character_items(id INTEGER PRIMARY KEY,"
            " character_guid INTEGER, item_entry INTEGER, slot INTEGER)")
        con.execute(
            "INSERT INTO item_templates(entry, spell_id1, spell_trigger1,"
            " spell_ppm_rate1) VALUES(647, 17152, 2, 1.0)")
        con.execute(
            "INSERT INTO character_items VALUES(1, 66, 647, 15)")
        con.commit()
        con.close()

        session = FakeUnit()
        session.config.database.world_path = path
        target = session.add_creature(22, 5.0)
        called = []

        async def fake_cast(spell_id, target_guid, source_key=0):
            called.append((spell_id, target_guid, source_key))

        session._cast_weapon_proc = fake_cast
        with mock.patch("random.random", return_value=0.0):
            await session._roll_weapon_procs(15, 2.0, target)
        assert called and called[0][0] == 17152


@_async_test
async def test_poison_enchant_and_proc_storage():
    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, "poison.db")
        con = sqlite3.connect(path)
        con.execute(
            "CREATE TABLE character_items(id INTEGER PRIMARY KEY,"
            " character_guid INTEGER, item_entry INTEGER, slot INTEGER,"
            " enchantment_2 INTEGER DEFAULT 0)")
        con.execute(
            "CREATE TABLE spell_item_enchantment(id INTEGER PRIMARY KEY,"
            " charges INTEGER DEFAULT 0, effect1 INTEGER DEFAULT 0,"
            " effect2 INTEGER DEFAULT 0, effect3 INTEGER DEFAULT 0,"
            " effect_points_min1 INTEGER DEFAULT 0,"
            " effect_points_min2 INTEGER DEFAULT 0,"
            " effect_points_min3 INTEGER DEFAULT 0,"
            " effect_arg1 INTEGER DEFAULT 0, effect_arg2 INTEGER DEFAULT 0,"
            " effect_arg3 INTEGER DEFAULT 0, name TEXT DEFAULT '')")
        con.execute(
            "CREATE TABLE spell_templates(id INTEGER PRIMARY KEY,"
            " name TEXT DEFAULT '', school INTEGER DEFAULT 0,"
            " duration_index INTEGER DEFAULT 0, base_level INTEGER DEFAULT 0,"
            " max_level INTEGER DEFAULT 0, range_index INTEGER DEFAULT 0,"
            " effect1 INTEGER DEFAULT 0, effect2 INTEGER DEFAULT 0,"
            " effect3 INTEGER DEFAULT 0, effect_base_points1 INTEGER DEFAULT 0,"
            " effect_base_points2 INTEGER DEFAULT 0,"
            " effect_base_points3 INTEGER DEFAULT 0,"
            " effect_die_sides1 INTEGER DEFAULT 0,"
            " effect_die_sides2 INTEGER DEFAULT 0,"
            " effect_die_sides3 INTEGER DEFAULT 0,"
            " effect_implicit_target_a1 INTEGER DEFAULT 0,"
            " effect_implicit_target_a2 INTEGER DEFAULT 0,"
            " effect_implicit_target_a3 INTEGER DEFAULT 0,"
            " effect_implicit_target_b1 INTEGER DEFAULT 0,"
            " effect_implicit_target_b2 INTEGER DEFAULT 0,"
            " effect_implicit_target_b3 INTEGER DEFAULT 0,"
            " effect_apply_aura_name1 INTEGER DEFAULT 0,"
            " effect_apply_aura_name2 INTEGER DEFAULT 0,"
            " effect_apply_aura_name3 INTEGER DEFAULT 0)")
        con.execute(
            "INSERT INTO spell_templates(id, name, effect1) VALUES(8680,"
            " 'Instant Poison', 2)")
        con.execute("INSERT INTO character_items VALUES(1, 66, 25, 15, 0)")
        con.execute(
            "INSERT INTO spell_item_enchantment VALUES("
            "323, 0, 1, 0, 0, 30, 0, 0, 8680, 0, 0, 'Instant Poison')")
        con.commit()
        con.close()

        session = FakeUnit()
        session.config.database.world_path = path
        template = spells.get_spell_template(WORLD_DB, 133)
        cast = spells.SpellCast(session.player_guid, 8679, 0)
        cast.item_guid = 1
        effect = {"misc_value": 323}
        await session._apply_weapon_enchant(cast, template, effect)
        entry = session._get_combat_state()["weapon_enchants"][15]
        assert entry["name"] == "Instant Poison"
        assert entry["procs"][0]["spell_id"] == 8680
        assert abs(entry["procs"][0]["ppm"] - 8.57) < 0.01
        # The temporary enchant id is persisted for the next login.
        con = sqlite3.connect(path)
        assert con.execute(
            "SELECT enchantment_2 FROM character_items WHERE id=1").fetchone()[0] == 323
        con.close()

    session = FakeUnit(char_class=8)
    session.character["power"] = 0
    restore = spells.get_spell_template(WORLD_DB, 436)
    cast = _cast(session, restore, session.player_guid)
    await session._apply_spell_effects(cast, restore, session.player_guid)
    assert session.character["power"] > 0
    assert any(op == int(Opcodes.SMSG_SPELLENERGIZELOG)
               for op, _ in session.sent)


@_async_test
async def test_damage_breaks_polymorph():
    session = FakeUnit()
    target = session.add_creature(9, 5.0)
    polymorph = spells.get_spell_template(WORLD_DB, 118)
    await session._apply_aura_from_effect(
        polymorph, polymorph.effects[0], 0, session.player_guid, target,
        20, target_kind="creature")
    assert session._creature_aura_mods(9)["polymorphed"]
    fireball = spells.get_spell_template(WORLD_DB, 133)
    await session._deal_spell_damage(
        target, 100, spells.SCHOOL_FIRE, fireball, session.player_guid)
    assert not session._creature_aura_mods(9)["polymorphed"]


@_async_test
async def test_direct_energize_restores_power_and_logs():
    session = FakeUnit(char_class=8)
    session.character["power"] = 0
    restore = spells.get_spell_template(WORLD_DB, 436)
    cast = _cast(session, restore, session.player_guid)
    await session._apply_spell_effects(cast, restore, session.player_guid)
    assert session.character["power"] > 0
    assert any(op == int(Opcodes.SMSG_SPELLENERGIZELOG)
               for op, _ in session.sent)


@_async_test
async def test_creature_threat_table_and_social_help():
    session = FakeUnit()
    first = session.add_creature(7, 5.0)
    session.add_creature(8, 6.0)
    session._visible_creatures[7] = first
    session._visible_creatures[8] = creature.creature_guid(8, 1008)
    await session._deal_damage_to_creature(first, 50, session.player_guid)
    state = session._combat_state["aggro"][7]
    assert state["threat_table"][session.player_guid] >= 50
    # The nearby same-faction ally was called into the fight.
    assert 8 in session._combat_state["aggro"]


@_async_test
async def test_spell_power_scales_direct_damage():
    session = FakeUnit()
    target = session.add_creature(6, 5.0)
    fireball = spells.get_spell_template(WORLD_DB, 133)
    cast = _cast(session, fireball, target)
    session.character["spell_hit_chance"] = 100.0
    session.character["spell_crit_chance"] = 0.0
    session.character["spell_power"] = 0
    with mock.patch("random.uniform", return_value=1.0):
        await session._apply_spell_effects(cast, fireball, target)
    low = 500 - session._creature_health(target)
    session._set_creature_health(6, 500)
    session.character["spell_power"] = 100
    with mock.patch("random.randint", return_value=1):
        await session._apply_spell_effects(cast, fireball, target)
    high = 500 - session._creature_health(target)
    assert high > low


class _PassedLeash(Exception):
    """Raised by the patched AI-state lookup right after the leash check."""


@_async_test
async def test_fled_creature_does_not_reset_while_victim_stays_near_home():
    """A low-health humanoid that fled far from its spawn keeps fighting.

    ``Creature::CanCreatureAttack`` only measures the victim's distance from
    the spawn; the old rule also reset creatures standing 45+ yards from home,
    which every flee ending past that point triggered.
    """
    session = FakeUnit()
    session.add_creature(5, x=0.0)                # spawn / home at the origin
    session._spawns[5]["current_health"] = 50       # 10%: already fled once
    session._combat_state["aggro"][5] = {"fled": True, "fleeing": False}
    session.character["x"] = 50.0                  # victim 50 yd from home
    fled_to = (55.0, 0.0, 0.0)                     # creature 55 yd from home
    with mock.patch.object(session, "_creature_position", lambda low: fled_to), \
            mock.patch.object(session, "_reset_creature",
                              mock.AsyncMock()) as reset, \
            mock.patch.object(session, "_creature_ai_state",
                              mock.AsyncMock(side_effect=_PassedLeash),
                              create=True):
        try:
            await session._creature_combat_tick(5, time.monotonic())
        except _PassedLeash:
            pass
        reset.assert_not_called()

        # A victim beyond the 60-yard threat radius still leashes it home.
        session.character["x"] = 61.0
        await session._creature_combat_tick(5, time.monotonic())
        reset.assert_awaited_once_with(5)


def test_spell_template_lookup_is_cached():
    """Hot combat/aura lookups must not open SQLite on every tick."""
    spells.clear_caches()
    first = spells.get_spell_template(WORLD_DB, 133)     # Fireball
    assert first is not None
    second = spells.get_spell_template(WORLD_DB, 133)
    assert second is first


@_async_test
async def test_regenerate_ignores_a_session_logged_out_mid_tick():
    """Logout can clear ``character`` while a combat tick is awaiting."""
    class LoggedOut:
        character = None

    # The guarded method returns before touching any other session state.
    await StatsMixin._regenerate(LoggedOut(), time.monotonic())
