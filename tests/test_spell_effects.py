"""Spell effect families that create items and learn spells."""
from __future__ import annotations

import asyncio
import sqlite3
import types

from src.common.config import AuthConfig, Config, DBConfig, LogConfig, WorldConfig
from src.protocol.opcodes import Opcodes
from src.world import character_db, creature, gameobject, spells
from src.world.groups import Group, GroupMember
from src.world.realmsocket import WorldSocket


class FakeWriter:
    def write(self, data):
        pass

    async def drain(self):
        pass

    def get_extra_info(self, name):
        return ("127.0.0.1", 12345)

    def close(self):
        pass

    async def wait_closed(self):
        pass

    def is_closing(self):
        return False


def _socket(tmp_path):
    db = str(tmp_path / "world.db")
    creature.init_creature_db(db)
    gameobject.init_gameobject_db(db)
    character_db.init_world_db(db)
    character_db.init_character_store(db)
    con = sqlite3.connect(db)
    try:
        con.execute(
            "INSERT OR REPLACE INTO item_templates"
            "(entry, name1, stackable, display_id, quality)"
            " VALUES(5350, 'Conjured Water', 20, 100, 1)")
        con.execute(
            "INSERT OR REPLACE INTO spell_templates"
            "(id, name, effect1, effect_base_points1, effect_item_type1)"
            " VALUES(5504, 'Conjure Water', 24, 1, 5350)")
        con.execute(
            "INSERT OR REPLACE INTO spell_templates"
            "(id, name, effect1, effect_trigger_spell1, spell_level)"
            " VALUES(100, 'Learn Test Spell', 36, 200, 1)")
        con.execute(
            "INSERT OR REPLACE INTO spell_templates"
            "(id, name, spell_level) VALUES(200, 'Test Spell', 1)")
        con.execute(
            "INSERT OR REPLACE INTO spell_templates"
            "(id, name, effect1, effect_misc_value1, effect_base_points1)"
            " VALUES(300, 'Apprentice Test Profession', 44, 333, 0)")
        con.execute(
            "INSERT OR REPLACE INTO spell_templates"
            "(id, name, effect1, equipped_item_class,"
            " equipped_item_subclass_mask)"
            " VALUES(301, 'Test Proficiency', 60, 2, 32)")
        con.execute(
            "CREATE TABLE IF NOT EXISTS spell_target_position("
            "id INTEGER PRIMARY KEY, target_map INTEGER,"
            "target_position_x REAL, target_position_y REAL,"
            "target_position_z REAL, target_orientation REAL)")
        con.execute(
            "INSERT OR REPLACE INTO spell_templates"
            "(id, name, effect1) VALUES(400, 'Teleport Same Map', 5)")
        con.execute(
            "INSERT OR REPLACE INTO spell_templates"
            "(id, name, effect1) VALUES(401, 'Teleport Cross Map', 5)")
        con.execute(
            "INSERT OR REPLACE INTO spell_templates"
            "(id, name, effect1, effect_trigger_spell1)"
            " VALUES(402, 'Trigger Missile Test', 32, 200)")
        con.execute(
            "INSERT OR REPLACE INTO spell_templates"
            "(id, name, effect1, effect_misc_value1, duration_index)"
            " VALUES(403, 'Summon Object Test', 104, 5000, 0)")
        con.execute(
            "INSERT OR REPLACE INTO spell_templates"
            "(id, name, effect1, effect_apply_aura_name1,"
            " effect_base_points1, effect_misc_value1, duration_index)"
            " VALUES(404, 'Rating Aura', 6, 189, 20, 5, 1)")
        con.execute(
            "INSERT OR REPLACE INTO spell_templates"
            "(id, name, effect1, effect_apply_aura_name1,"
            " effect_base_points1, effect_trigger_spell1, duration_index)"
            " VALUES(405, 'Proc Aura', 6, 42, 100, 200, 1)")
        con.execute(
            "INSERT OR REPLACE INTO spell_duration"
            "(id, duration, duration_per_level, max_duration)"
            " VALUES(1, 10000, 0, 10000)")
        # Spell-modifier aura: +20 to effect 1 of family-3 flag-1 spells.
        con.execute(
            "INSERT OR REPLACE INTO spell_templates"
            "(id, name, effect1, effect_apply_aura_name1,"
            " effect_base_points1, effect_misc_value1,"
            " effect_spell_class_mask1_1, spell_family_name, duration_index)"
            " VALUES(406, 'Spell Mod Aura', 6, 107, 20, 3, 1, 3, 1)")
        con.execute(
            "INSERT OR REPLACE INTO spell_templates"
            "(id, name, effect1, effect_base_points1, spell_family_name,"
            " spell_family_flags)"
            " VALUES(407, 'Modified Damage', 2, 100, 3, 1)")
        con.execute(
            "INSERT OR REPLACE INTO spell_templates"
            "(id, name, effect1, effect_misc_value1)"
            " VALUES(408, 'Apply Test Glyph', 74, 141)")
        con.execute(
            "INSERT OR REPLACE INTO spell_templates"
            "(id, name, effect1, effect_trigger_spell1)"
            " VALUES(409, 'Force Cast Test', 140, 200)")
        con.execute(
            "INSERT OR REPLACE INTO spell_templates"
            "(id, name, effect1, effect_apply_aura_name1,"
            " effect_base_points1, effect_implicit_target_a1,"
            " effect_radius_index1, duration_index)"
            " VALUES(410, 'Party Aura Test', 35, 99, 10, 1, 12, 1)")
        con.execute(
            "INSERT OR REPLACE INTO spell_templates"
            "(id, name, effect1, effect_apply_aura_name1,"
            " effect_base_points1, effect_implicit_target_a1,"
            " effect_radius_index1, duration_index)"
            " VALUES(411, 'Raid Aura Test', 65, 99, 10, 1, 12, 1)")
        con.execute(
            "INSERT OR REPLACE INTO gameobject_templates"
            "(entry, type, display_id, name, size, faction, flags)"
            " VALUES(5000, 6, 100, 'Trap', 1.0, 0, 0)")
        con.execute(
            "INSERT OR REPLACE INTO spell_target_position"
            " VALUES(400, 0, 10.0, 20.0, 30.0, 1.0)")
        con.execute(
            "INSERT OR REPLACE INTO spell_target_position"
            " VALUES(401, 1, 40.0, 50.0, 60.0, 2.0)")
        con.commit()
    finally:
        con.close()
    cfg = Config(
        authserver=AuthConfig(), worldserver=WorldConfig(client_data=""),
        database=DBConfig(auth_path=str(tmp_path / "auth.db"), world_path=db),
        logging=LogConfig(level="WARNING", log_dir=str(tmp_path / "logs"),
                          console=False),
    )
    sent = []
    delivered = []

    async def send_packet(opcode, payload=b""):
        sent.append((int(opcode), bytes(payload)))

    async def deliver_item_changes(item_entry, changes):
        delivered.append((item_entry, changes))

    socket = WorldSocket(asyncio.StreamReader(), FakeWriter(), cfg)
    socket.send_packet = send_packet
    socket.send_packet_to_peers = send_packet
    socket._deliver_item_changes = deliver_item_changes
    socket.in_world = True
    socket.player_guid = 1
    socket.character = {
        "guid": 1, "name": "Tester", "class": 8, "race": 1, "level": 10,
        "map": 0, "zone": 12, "x": 0.0, "y": 0.0, "z": 0.0,
        "health": 100, "max_health": 100, "power": 100, "max_power": 100,
        "inventory": {},
    }
    return socket, db, sent, delivered


def test_create_item_effect_adds_and_delivers_items(tmp_path):
    async def flow():
        socket, db, _sent, delivered = _socket(tmp_path)
        template = spells.get_spell_template(db, 5504)
        await socket._apply_spell_effects(None, template, 0)
        assert delivered and delivered[-1][0] == 5350
        assert delivered[-1][1][0]["stack_count"] == 2
        con = sqlite3.connect(db)
        try:
            row = con.execute(
                "SELECT item_entry, stack_count FROM character_items"
                " WHERE character_guid = 1").fetchone()
        finally:
            con.close()
        assert row == (5350, 2)

    asyncio.run(flow())


def test_skill_step_and_proficiency_effects_persist(tmp_path):
    async def flow():
        socket, db, _sent, _delivered = _socket(tmp_path)
        socket.character["skills"] = {}
        await socket._apply_spell_effects(
            None, spells.get_spell_template(db, 300), 0)
        assert socket.character["skills"][333]["step"] == 1
        await socket._apply_spell_effects(
            None, spells.get_spell_template(db, 301), 0)
        assert socket.character["proficiencies"][2] == 32
        con = sqlite3.connect(db)
        try:
            skill = con.execute(
                "SELECT step FROM character_skills WHERE character_guid = 1"
                " AND skill = 333").fetchone()
            proficiency = con.execute(
                "SELECT subclass_mask FROM character_proficiencies"
                " WHERE character_guid = 1 AND item_class = 2").fetchone()
        finally:
            con.close()
        assert skill == (1,)
        assert proficiency == (32,)

    asyncio.run(flow())


def test_spell_modifier_force_cast_and_glyph_effects(tmp_path):
    async def flow():
        socket, db, _sent, _delivered = _socket(tmp_path)
        await socket._apply_spell_effects(
            None, spells.get_spell_template(db, 406), socket.player_guid)
        target = spells.get_spell_template(db, 407)
        effect = target.effects[0]
        assert socket._spell_effect_value(
            target, 0, effect, 1) == 122

        triggered = []

        async def fake_trigger(spell_id, target_guid):
            triggered.append((int(spell_id), int(target_guid)))

        socket._cast_triggered = fake_trigger
        await socket._apply_spell_effects(
            None, spells.get_spell_template(db, 408), 0)
        assert triggered[-1][0] == 54292

        triggered.clear()
        await socket._apply_spell_effects(
            None, spells.get_spell_template(db, 409), 0)
        assert triggered[-1] == (200, socket.player_guid)

    asyncio.run(flow())


def test_party_and_raid_area_auras_respect_subgroups_and_radius(tmp_path):
    async def flow():
        socket, db, _sent, _delivered = _socket(tmp_path)
        group = Group(None, 1, 1, "Self")
        group.raid = True
        positions = {1: (0.0, 0.0, 0.0, 0), 2: (5.0, 0.0, 0.0, 0),
                     3: (5.0, 0.0, 0.0, 1), 4: (200.0, 0.0, 0.0, 0)}
        sessions = [socket]
        for guid in (2, 3, 4):
            x, y, z, subgroup = positions[guid]
            group.members[guid] = GroupMember(guid, str(guid), subgroup)
            peer = types.SimpleNamespace(
                player_guid=guid,
                character={"guid": guid, "map": 0, "x": x, "y": y, "z": z},
            )
            sessions.append(peer)
        group.members[1] = GroupMember(1, "Self", 0)
        manager = types.SimpleNamespace(
            group_for=lambda _guid: group,
            member_sessions=lambda _group, _map_id: sessions)
        socket._group_manager = lambda: manager

        applied = []

        async def apply_aura(template, effect, index, caster_guid,
                             target_guid, caster_level, combo_points=0,
                             target_kind=None):
            applied.append(int(target_guid))

        for session in sessions:
            session._apply_periodic_aura = lambda *args, **kwargs: None
            session._apply_aura_from_effect = apply_aura

        await socket._apply_spell_effects(
            None, spells.get_spell_template(db, 410), 0)
        assert applied == [1, 2]

        applied.clear()
        await socket._apply_spell_effects(
            None, spells.get_spell_template(db, 411), 0)
        assert applied == [1, 2, 3]

    asyncio.run(flow())


def test_teleport_trigger_missile_and_summon_object_effects(tmp_path):
    async def flow():
        socket, db, sent, _delivered = _socket(tmp_path)
        # Same-map teleport uses the near-teleport handshake.
        await socket._apply_spell_effects(
            None, spells.get_spell_template(db, 400), 0)
        assert (socket.character["x"], socket.character["y"],
                socket.character["z"]) == (10.0, 20.0, 30.0)
        assert any(opcode == int(Opcodes.MSG_MOVE_TELEPORT_ACK)
                   for opcode, _payload in sent)

        # A different target map starts a worldport.
        sent.clear()
        await socket._apply_spell_effects(
            None, spells.get_spell_template(db, 401), 0)
        assert socket.character["map"] == 1
        assert socket._repop_pending is True
        assert sent[-1][0] == int(Opcodes.SMSG_NEW_WORLD)

        # Trigger missiles run the referenced spell through the trigger path.
        triggered = []

        async def fake_trigger(spell_id, target_guid):
            triggered.append((int(spell_id), int(target_guid)))

        socket._cast_triggered = fake_trigger
        await socket._apply_spell_effects(
            None, spells.get_spell_template(db, 402), 0)
        assert triggered == [(200, socket.player_guid)]

        # Summon objects get a runtime gameobject create/destroy pair.
        sent.clear()
        await socket._apply_spell_effects(
            None, spells.get_spell_template(db, 403), 0)
        assert socket._summoned_gameobjects()
        low = next(iter(socket._summoned_gameobjects()))
        assert any(opcode in (int(Opcodes.SMSG_UPDATE_OBJECT),
                              int(Opcodes.SMSG_COMPRESSED_UPDATE_OBJECT))
                   for opcode, _payload in sent)
        sent.clear()
        await socket._dismiss_all_gameobjects()
        assert not socket._summoned_gameobjects()
        assert any(opcode == int(Opcodes.SMSG_DESTROY_OBJECT)
                   for opcode, _payload in sent)

    asyncio.run(flow())


def test_rating_and_proc_trigger_auras(tmp_path):
    async def flow():
        socket, db, _sent, _delivered = _socket(tmp_path)
        await socket._apply_spell_effects(
            None, spells.get_spell_template(db, 404), socket.player_guid)
        assert socket._player_aura_mods()["ratings"]["hit"] == 21

        await socket._apply_spell_effects(
            None, spells.get_spell_template(db, 405), socket.player_guid)
        triggered = []

        async def fake_trigger(spell_id, target_guid):
            triggered.append((int(spell_id), int(target_guid)))

        socket._cast_triggered = fake_trigger
        await socket._proc_trigger_auras(0xDEADBEEF)
        assert triggered == [(200, 0xDEADBEEF)]

    asyncio.run(flow())


def test_learn_spell_effect_updates_spellbook_and_store(tmp_path):
    async def flow():
        socket, db, sent, _delivered = _socket(tmp_path)
        socket.known_spell_ids = set()
        template = spells.get_spell_template(db, 100)
        await socket._apply_spell_effects(None, template, 0)
        assert 200 in socket.known_spell_ids
        assert any(opcode == int(Opcodes.SMSG_LEARNED_SPELL)
                   for opcode, _payload in sent)
        con = sqlite3.connect(db)
        try:
            row = con.execute(
                "SELECT spell FROM character_spells WHERE character_guid = 1"
            ).fetchone()
        finally:
            con.close()
        assert row == (200,)

    asyncio.run(flow())
