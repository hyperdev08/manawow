"""Spell effect families that create items and learn spells."""
from __future__ import annotations

import asyncio
import sqlite3

from src.common.config import AuthConfig, Config, DBConfig, LogConfig, WorldConfig
from src.protocol.opcodes import Opcodes
from src.world import character_db, creature, spells
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
