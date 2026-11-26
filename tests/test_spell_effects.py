"""Spell effect families that create items and learn spells."""
from __future__ import annotations

import asyncio
import sqlite3
import struct
import time
import types

from src.common.config import AuthConfig, Config, DBConfig, LogConfig, WorldConfig
from src.protocol.opcodes import Opcodes
from src.world import character_db, creature, gameobject, spells
from src.world import update_fields as F
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
            "INSERT OR REPLACE INTO item_templates"
            "(entry, name1, stackable, display_id, quality)"
            " VALUES(6000, 'Craft Reagent', 20, 100, 1)")
        con.execute(
            "INSERT OR REPLACE INTO item_templates"
            "(entry, name1, stackable, display_id, quality)"
            " VALUES(6001, 'Crafted Product', 20, 101, 1)")
        con.execute(
            "INSERT OR REPLACE INTO spell_templates"
            "(id, name, effect1, effect_item_type1, reagent1,"
            " reagent_count1, attributes)"
            " VALUES(412, 'Craft Test', 24, 6001, 6000, 2, 32)")
        con.execute(
            "INSERT OR REPLACE INTO skill_line_ability"
            "(spell_id, skill_line, race_mask, class_mask, min_rank,"
            " trivial_high, trivial_low)"
            " VALUES(412, 165, 0, 0, 0, 60, 30)")
        con.execute(
            "INSERT OR REPLACE INTO spell_templates"
            "(id, name, effect1, effect_misc_value1, effect_base_points1)"
            " VALUES(413, 'Journeyman Riding Test', 118, 762, 1)")
        con.execute(
            "INSERT OR REPLACE INTO spell_templates"
            "(id, name, effect1, effect_item_type1)"
            " VALUES(414, 'Create Item 2 Test', 157, 6001)")
        con.execute(
            "INSERT OR REPLACE INTO spell_templates"
            "(id, name, effect1, effect_item_type1, requires_spell_focus)"
            " VALUES(415, 'Focus Craft Test', 24, 6001, 1)")
        con.execute(
            "INSERT OR REPLACE INTO spell_templates"
            "(id, name, effect1, effect_item_type1, totem_category1)"
            " VALUES(416, 'Tool Craft Test', 24, 6001, 162)")
        con.execute(
            "INSERT OR REPLACE INTO item_templates"
            "(entry, name1, stackable, display_id, quality, totem_category)"
            " VALUES(6002, 'Test Hammer', 1, 102, 1, 162)")
        con.execute(
            "INSERT OR REPLACE INTO item_templates"
            "(entry, name1, stackable, display_id, quality, totem_category)"
            " VALUES(6003, 'Test Pick', 1, 103, 1, 165)")
        con.execute(
            "INSERT OR REPLACE INTO spell_templates"
            "(id, name, effect1, effect_apply_aura_name1, effect_base_points1,"
            " effect_implicit_target_a1, effect_radius_index1, duration_index)"
            " VALUES(417, 'Friend Aura Test', 128, 29, 5, 1, 13, 1)")
        con.execute(
            "INSERT OR REPLACE INTO spell_templates"
            "(id, name, effect1, effect_apply_aura_name1, effect_base_points1,"
            " effect_implicit_target_a1, effect_radius_index1, duration_index)"
            " VALUES(418, 'Enemy Aura Test', 129, 29, 5, 1, 13, 1)")
        con.execute(
            "INSERT OR REPLACE INTO spell_templates"
            "(id, name, effect1, effect_apply_aura_name1, effect_base_points1,"
            " effect_implicit_target_a1, effect_radius_index1, duration_index,"
            " effect_amplitude1)"
            " VALUES(419, 'Pet Aura Test', 119, 20, 2, 1, 13, 1, 1000)")
        con.execute(
            "INSERT OR REPLACE INTO spell_templates"
            "(id, name, effect1, effect_base_points1, effect_misc_value1,"
            " effect_multiple_value1)"
            " VALUES(420, 'Power Burn Test', 62, 9, 0, 0.5)")
        con.execute(
            "INSERT OR REPLACE INTO spell_templates"
            "(id, name, effect1, effect_base_points1, effect_implicit_target_a1)"
            " VALUES(421, 'Heal Percent Test', 136, 1, 1)")
        con.execute(
            "INSERT OR REPLACE INTO spell_templates"
            "(id, name, effect1) VALUES(422, 'Jump Dest Test', 42)")
        con.execute(
            "INSERT OR REPLACE INTO spell_templates"
            "(id, name, effect1, effect_apply_aura_name1, effect_base_points1,"
            " effect_implicit_target_a1, mechanic, duration_index)"
            " VALUES(423, 'Mechanic Aura Test', 6, 12, 1, 1, 7, 1)")
        con.execute(
            "INSERT OR REPLACE INTO spell_templates"
            "(id, name, effect1, effect_misc_value1)"
            " VALUES(424, 'Dispel Mechanic Test', 108, 7)")
        con.execute(
            "INSERT OR REPLACE INTO spell_templates"
            "(id, name, effect1, effect_base_points1)"
            " VALUES(425, 'Inebriate Test', 100, 19)")
        con.execute(
            "INSERT OR REPLACE INTO spell_templates"
            "(id, name, effect1) VALUES(426, 'Charge Test', 96)")
        con.execute(
            "INSERT OR REPLACE INTO spell_templates"
            "(id, name, effect1, effect_misc_value1)"
            " VALUES(427, 'Kill Credit Test', 90, 6000)")
        con.execute(
            "INSERT OR REPLACE INTO spell_templates"
            "(id, name, effect1, effect_misc_value1)"
            " VALUES(428, 'Kill Credit Group Test', 134, 6000)")
        con.execute(
            "INSERT OR REPLACE INTO spell_templates"
            "(id, name, effect1, effect_apply_aura_name1, effect_base_points1,"
            " effect_implicit_target_a1, effect_radius_index1, duration_index)"
            " VALUES(434, 'Party Aura Real Test', 35, 29, 5, 1, 13, 1)")
        for spell_id, aura, misc, base, amplitude in (
                (429, 84, 0, 9, 0), (430, 85, 0, 9, 0),
                (431, 161, 0, 4, 0), (432, 89, 0, 2, 1000),
                (433, 20, 0, 2, 1000)):
            con.execute(
                "INSERT OR REPLACE INTO spell_templates"
                "(id, name, effect1, effect_apply_aura_name1,"
                " effect_misc_value1, effect_base_points1,"
                " effect_implicit_target_a1, effect_amplitude1, duration_index)"
                " VALUES(?, ?, 6, ?, ?, ?, 1, ?, 1)",
                (spell_id, f"Aura {aura} Test", aura, misc, base, amplitude))
        con.execute(
            "INSERT OR REPLACE INTO spell_templates"
            "(id, name, effect1, effect_apply_aura_name1,"
            " effect_implicit_target_a1, effect_base_points1,"
            " effect_amplitude1, effect2, effect_apply_aura_name2,"
            " effect_implicit_target_a2, effect_base_points2,"
            " effect_amplitude2, duration_index)"
            " VALUES(435, 'Drink Aura Test', 6, 85, 1, -1, 0,"
            " 6, 226, 1, 41, 2200, 1)")
        con.execute(
            "INSERT OR REPLACE INTO spell_templates"
            "(id, name, effect1, effect_apply_aura_name1, effect_base_points1,"
            " effect_implicit_target_a1, duration_index)"
            " VALUES(436, 'Scale Aura Test', 6, 61, 49, 1, 1)")
        con.execute(
            "INSERT OR REPLACE INTO spell_templates"
            "(id, name, effect1, effect_apply_aura_name1, effect_base_points1,"
            " effect_misc_value1, effect_implicit_target_a1, duration_index)"
            " VALUES(437, 'Base Resist Aura Test', 6, 142, 99, 1, 1, 1)")
        con.execute(
            "INSERT OR REPLACE INTO spell_templates"
            "(id, name, effect1, effect_apply_aura_name1, effect_base_points1,"
            " effect_implicit_target_a1, duration_index)"
            " VALUES(438, 'Block Value Aura Test', 6, 158, 1, 1, 1)")
        con.execute(
            "INSERT OR REPLACE INTO spell_templates"
            "(id, name, effect1, effect_apply_aura_name1, effect_base_points1,"
            " effect_misc_value1, effect_implicit_target_a1, duration_index)"
            " VALUES(439, 'Skill Aura Test', 6, 30, 9, 164, 1, 1)")
        con.execute(
            "INSERT OR REPLACE INTO spell_templates"
            "(id, name, effect1) VALUES(440, 'Spawn Effect Test', 46)")
        con.execute(
            "INSERT OR REPLACE INTO spell_templates"
            "(id, name, effect1, effect_misc_value1, duration_index)"
            " VALUES(441, 'Transmitted Object Test', 50, 5000, 1)")
        con.execute(
            "INSERT OR REPLACE INTO spell_templates"
            "(id, name, effect1, effect_misc_value1)"
            " VALUES(442, 'Activate Object Test', 86, 11)")
        con.execute(
            "INSERT OR REPLACE INTO spell_templates"
            "(id, name, effect1, effect_base_points1)"
            " VALUES(443, 'Gameobject Damage Test', 87, 999)")
        con.execute(
            "INSERT OR REPLACE INTO spell_templates"
            "(id, name, effect1, effect_apply_aura_name1, effect_base_points1,"
            " effect_misc_value1, effect_implicit_target_a1, duration_index)"
            " VALUES(444, 'Agility Aura Test', 6, 29, 9, 1, 1, 1)")
        con.execute(
            "INSERT OR REPLACE INTO spell_templates"
            "(id, name, effect1, effect_misc_value1, effect_base_points1,"
            " effect_multiple_value1)"
            " VALUES(445, 'Power Drain Test', 8, 0, 9, 0.5)")
        con.execute(
            "INSERT OR REPLACE INTO spell_templates"
            "(id, name, effect1, effect_misc_value1, effect_base_points1,"
            " effect_implicit_target_a1)"
            " VALUES(446, 'Energize Percent Test', 137, 0, 2, 1)")
        con.execute(
            "INSERT OR REPLACE INTO spell_templates"
            "(id, name, effect1, effect_misc_value1, effect_base_points1)"
            " VALUES(447, 'Reputation Test', 103, 529, 49)")
        con.execute(
            "INSERT OR REPLACE INTO spell_templates"
            "(id, name, effect1) VALUES(448, 'Leap Test', 29)")
        con.execute(
            "INSERT OR REPLACE INTO spell_templates"
            "(id, name, effect1) VALUES(449, 'Jump Test', 41)")
        con.execute(
            "INSERT OR REPLACE INTO spell_templates"
            "(id, name, effect1) VALUES(450, 'Pull Test', 124)")
        con.execute(
            "INSERT OR REPLACE INTO spell_templates"
            "(id, name, effect1, effect_apply_aura_name1, effect_misc_value1,"
            " effect_base_points1, effect_implicit_target_a1, duration_index)"
            " VALUES(451, 'Mount Aura Test', 6, 78, 284, 0, 1, 1)")
        con.execute(
            "INSERT OR REPLACE INTO spell_templates"
            "(id, name, effect1, effect_apply_aura_name1, effect_base_points1,"
            " effect_implicit_target_a1, duration_index)"
            " VALUES(452, 'Mount Speed Test', 6, 32, 59, 1, 1)")
        con.execute(
            "INSERT OR REPLACE INTO spell_templates"
            "(id, name, effect1, effect_apply_aura_name1, effect_base_points1,"
            " effect_implicit_target_a1, duration_index)"
            " VALUES(453, 'Disarm Aura Test', 6, 67, -1, 1, 1)")
        con.execute(
            "INSERT OR REPLACE INTO spell_templates"
            "(id, name, effect1, effect_apply_aura_name1, effect_base_points1,"
            " effect_implicit_target_a1, duration_index)"
            " VALUES(454, 'Taunt Aura Test', 6, 11, -1, 6, 1)")
        con.execute(
            "INSERT OR REPLACE INTO spell_templates"
            "(id, name, effect1, effect_apply_aura_name1, effect_base_points1,"
            " effect_implicit_target_a1, duration_index)"
            " VALUES(455, 'Proc Damage Aura Test', 6, 43, 5, 1, 1)")
        con.execute(
            "INSERT OR REPLACE INTO spell_templates"
            "(id, name, effect1, effect_apply_aura_name1, effect_misc_value1,"
            " effect_base_points1, effect_implicit_target_a1, duration_index)"
            " VALUES(456, 'Invisibility Aura Test', 6, 18, 0, 10, 1, 1)")
        con.execute(
            "INSERT OR REPLACE INTO spell_templates"
            "(id, name, effect1, effect_apply_aura_name1, effect_misc_value1,"
            " effect_base_points1, effect_implicit_target_a1, duration_index)"
            " VALUES(457, 'Invisibility Detect Test', 6, 19, 0, 10, 1, 1)")
        con.execute(
            "INSERT OR REPLACE INTO spell_templates"
            "(id, name, effect1, effect_apply_aura_name1, effect_misc_value1,"
            " effect_base_points1, effect_implicit_target_a1, duration_index)"
            " VALUES(458, 'Force Reaction Test', 6, 139, 529, 3, 1, 1)")
        con.execute(
            "INSERT OR REPLACE INTO spell_templates"
            "(id, name, effect1, effect_apply_aura_name1, effect_base_points1,"
            " effect_trigger_spell1, spell_family_name,"
            " effect_implicit_target_a1, duration_index)"
            " VALUES(459, 'Target Trigger Aura Test', 6, 109, 99, 200, 3,"
            " 1, 1)")
        con.execute(
            "INSERT OR REPLACE INTO spell_templates"
            "(id, name, attributes, effect1, effect_apply_aura_name1,"
            " effect_base_points1, effect_misc_value1)"
            " VALUES(11069, 'Improved Fireball', 262608, 6, 107, -101, 10)")
        con.execute(
            "INSERT OR REPLACE INTO spell_templates"
            "(id, name, effect1, effect_trigger_spell1)"
            " VALUES(460, 'Learn Pet Spell Test', 57, 200)")
        con.execute(
            "INSERT OR REPLACE INTO spell_templates"
            "(id, name, effect1) VALUES(461, 'Sanctuary Test', 79)")
        con.execute(
            "INSERT OR REPLACE INTO spell_templates"
            "(id, name, effect1, effect_misc_value1)"
            " VALUES(462, 'Create Tamed Pet Test', 153, 284)")
        con.execute(
            "INSERT OR REPLACE INTO spell_templates"
            "(id, name, effect1, effect_base_points1)"
            " VALUES(463, 'Add Honor Test', 45, 99)")
        con.execute(
            "INSERT OR REPLACE INTO spell_templates"
            "(id, name, effect1, effect_trigger_spell1)"
            " VALUES(464, 'Unlearn Spec Test', 133, 200)")
        con.execute(
            "INSERT OR REPLACE INTO creature_templates"
            "(entry, name, display_id1, scale, min_level, max_level, visible)"
            " VALUES(284, 'Brown Horse', 2404, 1.0, 1, 1, 1)")
        con.execute(
            "INSERT OR REPLACE INTO gameobject_templates"
            "(entry, type, display_id, name, size, faction, flags)"
            " VALUES(5000, 6, 100, 'Trap', 1.0, 0, 0)")
        con.execute(
            "INSERT OR REPLACE INTO gameobject_templates"
            "(entry, type, display_id, name, size, faction, flags,"
            " data0, data1)"
            " VALUES(5001, 33, 101, 'Wall', 1.0, 0, 0, 5000, 0)")
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


def test_skill_effect_sets_rank_and_create_item_2(tmp_path):
    async def flow():
        socket, db, _sent, delivered = _socket(tmp_path)
        socket.character["skills"] = {}
        await socket._apply_spell_effects(
            None, spells.get_spell_template(db, 413), 0)
        riding = socket.character["skills"][762]
        assert riding["step"] == 2
        assert riding["max_value"] == 150

        await socket._apply_spell_effects(
            None, spells.get_spell_template(db, 414), 0)
        assert delivered and delivered[-1][0] == 6001
        con = sqlite3.connect(db)
        try:
            row = con.execute(
                "SELECT stack_count FROM character_items"
                " WHERE character_guid = 1 AND item_entry = 6001").fetchone()
        finally:
            con.close()
        assert row == (1,)

    asyncio.run(flow())


def _load_inventory(socket, db):
    inventory = character_db.get_character_inventory(db, 1)
    socket.character["inventory"] = {
        int(slot): {**dict(item),
                    "guid": F.ITEM_HIGH_GUID | int(item["id"])}
        for slot, item in inventory.items()
    }


def _last_failure(sent):
    failures = [payload for opcode, payload in sent
                if opcode == int(Opcodes.SMSG_CAST_FAILED)]
    return failures[-1][5] if failures else None


def test_casting_requires_spell_focus_and_tools(tmp_path):
    async def flow():
        socket, db, sent, _delivered = _socket(tmp_path)
        socket.known_spell_ids = {415, 416}
        spawns = []

        async def gameobjects_near(_point):
            return list(spawns)

        socket._gameobjects_near = gameobjects_near

        await socket.handle_cast_spell(struct.pack("<BiBI", 1, 415, 0, 0))
        assert _last_failure(sent) == 102

        spawns.append({"type": 8, "data0": 1, "x": 1.0, "y": 0.0, "z": 0.0})
        sent.clear()
        await socket.handle_cast_spell(struct.pack("<BiBI", 2, 415, 0, 0))
        assert _last_failure(sent) is None

        # A hammer (162) is required; a mining pick (165) is not compatible.
        con = sqlite3.connect(db)
        try:
            con.execute(
                "DELETE FROM character_items WHERE character_guid = 1")
            con.commit()
        finally:
            con.close()
        assert character_db.add_item_to_inventory(db, 1, 6003, 1)
        _load_inventory(socket, db)
        sent.clear()
        await socket.handle_cast_spell(struct.pack("<BiBI", 3, 416, 0, 0))
        assert _last_failure(sent) == 130

        con = sqlite3.connect(db)
        try:
            pick_slot = con.execute(
                "SELECT slot FROM character_items"
                " WHERE character_guid = 1 AND item_entry = 6003").fetchone()[0]
        finally:
            con.close()
        character_db.delete_character_item(db, 1, int(pick_slot))
        assert character_db.add_item_to_inventory(db, 1, 6002, 1)
        _load_inventory(socket, db)
        sent.clear()
        await socket.handle_cast_spell(struct.pack("<BiBI", 4, 416, 0, 0))
        assert _last_failure(sent) is None

    asyncio.run(flow())


def test_full_bag_fails_create_item_without_consuming_reagents(tmp_path):
    async def flow():
        socket, db, sent, _delivered = _socket(tmp_path)
        # The reagent stack fills one slot; single-item stacks fill the rest, so
        # the reagent requirement is met but the product cannot be stored.
        con = sqlite3.connect(db)
        try:
            con.execute(
                "INSERT INTO character_items(character_guid, item_entry, slot,"
                " stack_count) VALUES(1, 6000, 256, 2)")
            for slot in range(257, 272):
                con.execute(
                    "INSERT INTO character_items(character_guid, item_entry,"
                    " slot, stack_count) VALUES(1, 6003, ?, 1)", (slot,))
            con.commit()
        finally:
            con.close()
        _load_inventory(socket, db)
        assert not character_db.can_add_item_to_inventory(db, 1, 6001, 1)
        socket.known_spell_ids = {412}
        socket.character["skills"] = {
            165: {"slot": 1, "value": 10, "max_value": 75, "step": 1}
        }
        await socket.handle_cast_spell(struct.pack("<BiBI", 1, 412, 0, 0))
        assert _last_failure(sent) is None
        con = sqlite3.connect(db)
        try:
            reagents = con.execute(
                "SELECT SUM(stack_count) FROM character_items"
                " WHERE character_guid = 1 AND item_entry = 6000").fetchone()
            products = con.execute(
                "SELECT COUNT(*) FROM character_items"
                " WHERE character_guid = 1 AND item_entry = 6001").fetchone()
        finally:
            con.close()
        assert reagents == (2,)          # untouched on failure
        assert products == (0,)
        assert socket.character["skills"][165]["value"] == 10

    asyncio.run(flow())


def test_crafting_consumes_reagents_creates_item_and_skills_up(tmp_path):
    async def flow():
        socket, db, sent, _delivered = _socket(tmp_path)
        changes = character_db.add_item_to_inventory(db, 1, 6000, 3)
        assert changes
        inventory = character_db.get_character_inventory(db, 1)
        socket.character["inventory"] = {
            int(slot): {**dict(item),
                        "guid": F.ITEM_HIGH_GUID | int(item["id"])}
            for slot, item in inventory.items()
        }
        socket.character["skills"] = {
            165: {"slot": 1, "value": 10, "max_value": 75, "step": 1}
        }
        socket.known_spell_ids = {412}
        await socket.handle_cast_spell(struct.pack("<BiBI", 1, 412, 0, 0))
        assert sent
        con = sqlite3.connect(db)
        try:
            reagent = con.execute(
                "SELECT stack_count FROM character_items"
                " WHERE character_guid = 1 AND item_entry = 6000").fetchone()
            product = con.execute(
                "SELECT stack_count FROM character_items"
                " WHERE character_guid = 1 AND item_entry = 6001").fetchone()
        finally:
            con.close()
        assert reagent == (1,)
        assert product == (1,)
        assert socket.character["skills"][165]["value"] == 11

        # Missing reagents must fail before creating anything.
        socket.character["inventory"].clear()
        sent.clear()
        await socket.handle_cast_spell(struct.pack("<BiBI", 2, 412, 0, 0))
        failures = [payload for opcode, payload in sent
                    if opcode == int(Opcodes.SMSG_CAST_FAILED)]
        assert failures and failures[-1][5] == 100

    asyncio.run(flow())


def test_area_auras_friend_enemy_and_pet(tmp_path):
    async def flow():
        socket, db, _sent, _delivered = _socket(tmp_path)
        peer_applied = []

        async def peer_aura(template, effect, index, caster_guid,
                            target_guid, caster_level, combo_points=0,
                            target_kind=None):
            peer_applied.append((int(template.id), int(target_guid)))

        near = types.SimpleNamespace(
            player_guid=2, character={"x": 5.0, "y": 0.0, "z": 0.0},
            _apply_periodic_aura=lambda *a, **k: None,
            _apply_aura_from_effect=peer_aura)
        far = types.SimpleNamespace(
            player_guid=3, character={"x": 30.0, "y": 0.0, "z": 0.0},
            _apply_periodic_aura=lambda *a, **k: None,
            _apply_aura_from_effect=peer_aura)
        socket.peers = lambda: [near, far]

        await socket._apply_spell_effects(
            None, spells.get_spell_template(db, 417), 0)
        assert peer_applied == [(417, 2)]
        auras = socket._get_combat_state()["auras"]
        assert any(int(a["spell_id"]) == 417 for a in auras)

        # Party area auras use the same component lookup as plain auras.
        await socket._apply_spell_effects(
            None, spells.get_spell_template(db, 434), 0)
        assert any(int(a["spell_id"]) == 434
                   for a in socket._get_combat_state()["auras"])

        # Enemy aura debuffs hostile creatures but leaves service NPCs alone.
        combat = socket._get_combat_state()
        combat["creature_auras"].clear()
        socket._creature_spawn_cache = {
            5000: {"guid": 5000, "entry": 5000, "x": 3.0, "y": 0.0,
                    "z": 0.0, "dead_since": 0, "npc_flags": 0,
                    "unit_flags": 0, "min_level": 1, "max_level": 1,
                    "current_health": 100, "max_health": 100,
                    "combat_reach": 0.5, "bounding_radius": 0.5,
                    "faction": 0, "name": "Dummy"},
        }
        await socket._apply_spell_effects(
            None, spells.get_spell_template(db, 418), 0)
        entries = combat["creature_auras"].get(5000, [])
        assert any(int(a["spell_id"]) == 418 for a in entries)

        # Pet aura applies to the active pet and registers its periodic heal.
        pet_spawn = dict(socket._creature_spawn_cache[5000])
        pet_spawn.update({"entry": 5001, "name": "Pet", "is_pet": True,
                          "expires": None})
        socket._summoned_creatures = {6000: pet_spawn}
        socket._summon_states = {
            6000: {"is_pet": True, "stationary": False}}
        await socket._apply_spell_effects(
            None, spells.get_spell_template(db, 419), 0)
        pet_auras = combat["creature_auras"].get(6000, [])
        assert any(int(a["spell_id"]) == 419 for a in pet_auras)
        dots = [d for d in combat["dots"] if int(d["spell_id"]) == 419]
        assert dots and dots[0]["percent"] and dots[0]["is_heal"]

    asyncio.run(flow())


def test_power_burn_heal_pct_jump_and_dispel_mechanic(tmp_path):
    async def flow():
        socket, db, _sent, _delivered = _socket(tmp_path)
        socket.character["power"] = 100
        socket.character["health"] = 100
        await socket._apply_spell_effects(
            None, spells.get_spell_template(db, 420), socket.player_guid)
        assert socket.character["power"] == 90
        assert socket.character["health"] == 95

        socket.character["health"] = 50
        await socket._apply_spell_effects(
            None, spells.get_spell_template(db, 421), socket.player_guid)
        assert socket.character["health"] == 52

        cast = types.SimpleNamespace(has_dest=True, x=5.0, y=6.0, z=0.0,
                                     item_guid=0)
        await socket._apply_spell_effects(
            cast, spells.get_spell_template(db, 422), 0)
        assert (socket.character["x"], socket.character["y"]) == (5.0, 6.0)

        await socket._apply_spell_effects(
            None, spells.get_spell_template(db, 423), socket.player_guid)
        assert any(int(a["spell_id"]) == 423
                   for a in socket._get_combat_state()["auras"])
        await socket._apply_spell_effects(
            None, spells.get_spell_template(db, 424), socket.player_guid)
        assert not any(int(a["spell_id"]) == 423
                       for a in socket._get_combat_state()["auras"])

    asyncio.run(flow())


def test_inebriate_kill_credit_and_charge(tmp_path):
    async def flow():
        socket, db, sent, _delivered = _socket(tmp_path)
        await socket._apply_spell_effects(
            None, spells.get_spell_template(db, 425), socket.player_guid)
        assert socket.character["drunk"] == 20
        assert int(Opcodes.SMSG_CROSSED_INEBRIATION_THRESHOLD) in [
            opcode for opcode, _payload in sent]

        credited = []

        async def credit(entry, guid=0):
            credited.append((int(entry), int(guid)))
        socket._quest_credit_kill_entry = credit
        await socket._apply_spell_effects(
            None, spells.get_spell_template(db, 427), socket.player_guid)
        assert credited == [(6000, 0)]

        peer_credit = []

        async def peer_credit_entry(entry, guid=0):
            peer_credit.append((int(entry), int(guid)))

        peer = types.SimpleNamespace(
            player_guid=2, character={"x": 1.0, "y": 0.0, "z": 0.0},
            _quest_credit_kill_entry=peer_credit_entry)
        socket._group_member_sessions = lambda exclude_self=False: [peer]
        await socket._apply_spell_effects(
            None, spells.get_spell_template(db, 428), socket.player_guid)
        assert credited[-1] == (6000, 0)
        assert len(peer_credit) == 1

        combat = socket._get_combat_state()
        spawn = {"guid": 5000, "entry": 5000, "x": 10.0, "y": 0.0,
                 "z": 0.0, "dead_since": 0, "npc_flags": 0,
                 "unit_flags": 0, "min_level": 1, "max_level": 1,
                 "current_health": 100, "max_health": 100,
                 "combat_reach": 0.0, "bounding_radius": 0.5,
                 "faction": 0, "name": "Dummy"}
        socket._summoned_creatures = {5000: spawn}
        socket._get_creature_spawn = lambda low: (
            spawn if int(low) == 5000 else None)
        attacked = []

        async def attack(payload):
            attacked.append(payload)
        socket.handle_attack_swing = attack
        guid = socket._creature_object_guid(5000, 5000)
        await socket._apply_spell_effects(
            None, spells.get_spell_template(db, 426), guid)
        assert socket.character["x"] > 6.0
        assert attacked

    asyncio.run(flow())


def test_food_drink_and_percent_regen_auras(tmp_path):
    async def flow():
        socket, db, _sent, _delivered = _socket(tmp_path)
        now = time.monotonic()
        socket.character["health"] = 50
        socket.character["stats"] = {"spi": 0}
        socket.character["level"] = 1
        await socket._apply_spell_effects(
            None, spells.get_spell_template(db, 429), socket.player_guid)
        combat = socket._get_combat_state()
        combat["last_health_gain"] = now - 2.0
        await socket._regenerate(now)
        assert socket.character["health"] >= 54

        socket.character["health"] = 100
        socket.character["power"] = 50
        await socket._apply_spell_effects(
            None, spells.get_spell_template(db, 430), socket.player_guid)
        combat["last_mana_gain"] = now - 2.0
        await socket._regenerate(now)
        assert socket.character["power"] >= 54

        mods = socket._player_aura_mods()
        assert mods["health_regen"] == 10
        assert mods["power_regen"][0] == 10

        socket.character["health"] = 50
        await socket._apply_spell_effects(
            None, spells.get_spell_template(db, 433), socket.player_guid)
        dot = next(d for d in combat["dots"]
                   if int(d["spell_id"]) == 433)
        await socket._apply_dot_tick(dot)
        assert socket.character["health"] == 53

        socket.character["health"] = 100
        await socket._apply_spell_effects(
            None, spells.get_spell_template(db, 432), socket.player_guid)
        dot = next(d for d in combat["dots"]
                   if int(d["spell_id"]) == 432)
        await socket._apply_dot_tick(dot)
        assert socket.character["health"] == 97

    asyncio.run(flow())


def test_drink_aura_and_easy_stat_auras(tmp_path):
    async def flow():
        socket, db, _sent, _delivered = _socket(tmp_path)
        await socket._apply_spell_effects(
            None, spells.get_spell_template(db, 435), socket.player_guid)
        mods = socket._player_aura_mods()
        assert mods["power_regen"].get(0) == 42

        socket.character["skills"] = {
            164: {"slot": 1, "value": 5, "max_value": 75, "step": 1}}
        await socket._apply_spell_effects(
            None, spells.get_spell_template(db, 444), socket.player_guid)
        await socket._apply_spell_effects(
            None, spells.get_spell_template(db, 436), socket.player_guid)
        assert abs(socket.character["object_scale"] - 1.5) < 1e-6
        armor_before = int(socket.character["armor"])
        assert armor_before > 0
        await socket._apply_spell_effects(
            None, spells.get_spell_template(db, 437), socket.player_guid)
        assert socket.character["armor"] == armor_before * 2
        await socket._apply_spell_effects(
            None, spells.get_spell_template(db, 438), socket.player_guid)
        assert socket.character["block_value"] == 2
        await socket._apply_spell_effects(
            None, spells.get_spell_template(db, 439), socket.player_guid)
        assert socket._effective_skills()[164]["value"] == 15
        assert socket._player_skills()[164]["value"] == 5

    asyncio.run(flow())


def test_transmitted_activate_and_damage_gameobjects(tmp_path):
    async def flow():
        socket, db, sent, _delivered = _socket(tmp_path)
        await socket._apply_spell_effects(
            None, spells.get_spell_template(db, 440), 0)
        cast = types.SimpleNamespace(has_dest=True, x=4.0, y=5.0, z=0.0,
                                     item_guid=0)
        await socket._apply_spell_effects(
            cast, spells.get_spell_template(db, 441), 0)
        objects = socket._summoned_gameobjects()
        assert objects
        low, spawn = next(iter(objects.items()))
        assert int(spawn["entry"]) == 5000
        assert (float(spawn["x"]), float(spawn["y"])) == (4.0, 5.0)

        guid = gameobject.gameobject_guid(low, 5000)
        socket._visible_gameobjects[low] = guid
        await socket._apply_spell_effects(
            None, spells.get_spell_template(db, 442), guid)
        assert socket._go_runtime().state(low) == gameobject.GO_STATE_ACTIVE

        wall_low = await socket._summon_gameobject(5001, 0.0, 0.0, 0.0, 0.0,
                                                   60000)
        wall_guid = gameobject.gameobject_guid(wall_low, 5001)
        socket._visible_gameobjects[wall_low] = wall_guid
        wall_spawn = socket._summoned_gameobjects()[wall_low]
        sent.clear()
        await socket._apply_spell_effects(
            None, spells.get_spell_template(db, 443), wall_guid)
        runtime = socket._go_runtime()
        assert runtime.current_health(wall_low, wall_spawn) == 4000
        assert int(Opcodes.SMSG_DESTRUCTIBLE_BUILDING_DAMAGE) in [
            opcode for opcode, _payload in sent]

    asyncio.run(flow())


def test_mount_disarm_invisibility_and_force_reaction_auras(tmp_path):
    async def flow():
        socket, db, sent, _delivered = _socket(tmp_path)
        await socket._apply_spell_effects(
            None, spells.get_spell_template(db, 451), socket.player_guid)
        await socket._apply_spell_effects(
            None, spells.get_spell_template(db, 452), socket.player_guid)
        state = socket._get_combat_state()["aura_state"]
        assert state["mount_display"] == 2404
        assert state["speed"] > 11.0

        mods = socket._player_aura_mods()
        assert mods["mounted"] == 284
        assert mods["mounted_speed"] == 60

        await socket._apply_spell_effects(
            None, spells.get_spell_template(db, 453), socket.player_guid)
        assert socket._player_aura_mods()["disarmed"]

        await socket._apply_spell_effects(
            None, spells.get_spell_template(db, 456), socket.player_guid)
        await socket._apply_spell_effects(
            None, spells.get_spell_template(db, 457), socket.player_guid)
        mods = socket._player_aura_mods()
        assert mods["invisibility"].get(0) == 11
        assert mods["invisibility_detect"].get(0) == 11

        await socket._apply_spell_effects(
            None, spells.get_spell_template(db, 458), socket.player_guid)
        assert socket._get_combat_state()["aura_state"][
            "forced_reactions"] == [(529, 4)]
        assert int(Opcodes.SMSG_SET_FORCED_REACTIONS) in [
            opcode for opcode, _payload in sent]

    asyncio.run(flow())


def test_power_drain_energize_reputation_and_jump_effects(tmp_path):
    async def flow():
        socket, db, _sent, _delivered = _socket(tmp_path)
        socket.character["power"] = 100
        await socket._apply_spell_effects(
            None, spells.get_spell_template(db, 445), socket.player_guid)
        assert socket.character["power"] == 90

        socket.character["power"] = 50
        await socket._apply_spell_effects(
            None, spells.get_spell_template(db, 446), socket.player_guid)
        assert socket.character["power"] == 53

        await socket._apply_spell_effects(
            None, spells.get_spell_template(db, 447), socket.player_guid)
        assert socket.character["reputation"][529] == 50

        cast = types.SimpleNamespace(has_dest=True, x=7.0, y=8.0, z=0.0,
                                     item_guid=0)
        await socket._apply_spell_effects(
            cast, spells.get_spell_template(db, 448), 0)
        assert (socket.character["x"], socket.character["y"]) == (7.0, 8.0)

        spawn = {"guid": 5000, "entry": 5000, "x": 10.0, "y": 0.0,
                 "z": 0.0, "dead_since": 0, "npc_flags": 0,
                 "unit_flags": 0, "min_level": 1, "max_level": 1,
                 "current_health": 100, "max_health": 100,
                 "combat_reach": 0.0, "bounding_radius": 0.5,
                 "faction": 0, "name": "Dummy", "scale": 1.0}
        socket._summoned_creatures = {5000: spawn}
        socket._get_creature_spawn = lambda low: (
            spawn if int(low) == 5000 else None)
        guid = socket._creature_object_guid(5000, 5000)
        await socket._apply_spell_effects(
            None, spells.get_spell_template(db, 449), guid)
        assert socket.character["x"] != 10.0
        sent_before = len(_sent)
        await socket._apply_spell_effects(
            None, spells.get_spell_template(db, 450), guid)
        assert len(_sent) > sent_before

    asyncio.run(flow())


def test_taunt_proc_damage_and_target_trigger_auras(tmp_path):
    async def flow():
        socket, db, _sent, _delivered = _socket(tmp_path)
        spawn = {"guid": 5000, "entry": 5000, "x": 10.0, "y": 0.0,
                 "z": 0.0, "dead_since": 0, "npc_flags": 0,
                 "unit_flags": 0, "min_level": 1, "max_level": 1,
                 "current_health": 100, "max_health": 100,
                 "combat_reach": 0.0, "bounding_radius": 0.5,
                 "faction": 0, "name": "Dummy", "scale": 1.0}
        socket._summoned_creatures = {}
        socket._get_creature_spawn = lambda low: (
            spawn if int(low) == 5000 else None)
        guid = socket._creature_object_guid(5000, 5000)

        await socket._apply_spell_effects(
            None, spells.get_spell_template(db, 454), guid)
        state = socket._get_combat_state()["aggro"].get(5000)
        assert state is not None
        assert int(state["taunt_target"]) == int(socket.player_guid)
        socket._fade_taunt_auras(5000, [
            aura for aura in socket._get_combat_state()["creature_auras"][5000]
            if int(aura["spell_id"]) == 454])
        assert not int(state["taunt_target"])

        await socket._apply_spell_effects(
            None, spells.get_spell_template(db, 455), socket.player_guid)
        damage = []

        async def record_damage(target, amount, school, template, caster,
                                **kwargs):
            damage.append((int(target), int(amount)))
        socket._deal_spell_damage = record_damage
        await socket._proc_trigger_auras(1234)
        assert damage and damage[0] == (1234, 6)

        await socket._apply_spell_effects(
            None, spells.get_spell_template(db, 459), socket.player_guid)
        triggered = []

        async def record_trigger(spell_id, target_guid):
            triggered.append((int(spell_id), int(target_guid)))
        socket._cast_triggered = record_trigger
        await socket._apply_spell_effects(
            None, spells.get_spell_template(db, 407), guid)
        assert (200, int(guid)) in triggered

    asyncio.run(flow())


def test_pet_spell_sanctuary_honor_and_unlearn_effects(tmp_path):
    async def flow():
        socket, db, sent, _delivered = _socket(tmp_path)
        await socket._apply_spell_effects(
            None, spells.get_spell_template(db, 462), socket.player_guid)
        pet = socket._active_pet()
        assert pet is not None
        low, spawn, state = pet
        pet_guid = socket._creature_object_guid(low, int(spawn["entry"]))
        await socket._apply_spell_effects(
            None, spells.get_spell_template(db, 460), pet_guid)
        assert 200 in state["pet_spells"]

        await socket._apply_spell_effects(
            None, spells.get_spell_template(db, 463), socket.player_guid)
        assert socket.character["honor"] == 100

        combat = socket._get_combat_state()
        combat["aggro"] = {5000: {
            "guid": 5000, "threat_table": {1: 10.0},
            "taunt_target": 1, "taunt_until": time.monotonic() + 5.0,
            "target_guid": 1,
        }}
        combat["target_guid"] = 1
        combat["auto_attacking"] = True
        await socket._apply_spell_effects(
            None, spells.get_spell_template(db, 461), socket.player_guid)
        assert 5000 not in combat["aggro"]
        assert not combat["auto_attacking"]

        socket.known_spell_ids = {200}
        sent.clear()
        await socket._apply_spell_effects(
            None, spells.get_spell_template(db, 464), socket.player_guid)
        assert 200 not in socket.known_spell_ids
        assert int(Opcodes.SMSG_REMOVED_SPELL) in [
            opcode for opcode, _payload in sent]

    asyncio.run(flow())


def test_talent_learning_points_and_reset(tmp_path):
    async def flow():
        socket, db, sent, _delivered = _socket(tmp_path)
        socket.character.update({"level": 10, "class": 8, "talents": {}})
        socket.known_spell_ids = set()

        assert await socket._learn_talent(26, 0)
        assert 11069 in socket.known_spell_ids
        assert socket.character["talents"] == {26: 0}
        assert int(Opcodes.SMSG_TALENTS_INFO) in [
            opcode for opcode, _payload in sent]
        assert any(int(aura["spell_id"]) == 11069
                   for aura in socket._get_combat_state()["auras"])

        # Tier 2 needs 10 points in the tree; one point is not enough.
        assert not await socket._learn_talent(29, 0)

        await socket._reset_talents()
        assert socket.character["talents"] == {}
        assert 11069 not in socket.known_spell_ids
        assert not any(int(aura["spell_id"]) == 11069
                       for aura in socket._get_combat_state()["auras"])

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
