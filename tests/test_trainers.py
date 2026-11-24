"""CMaNGOS-driven NPC spell trainers: rows, states and packets."""
from __future__ import annotations

import asyncio
import sqlite3
import struct
import types

from src.protocol.opcodes import Opcodes
from src.world import character_db, creature, object_update, spells, trainer
from src.world.worldsocket.characters import CharactersMixin
from src.world.worldsocket.trainers import TrainersMixin


def _world_db(tmp_path):
    db = str(tmp_path / "world.db")
    creature.init_creature_db(db)
    character_db.init_character_store(db)
    character_db.configure(db, db)
    con = sqlite3.connect(db)
    try:
        # Two mage spells plus their trainer row.
        con.executemany(
            "INSERT OR REPLACE INTO spell_templates"
            "(id, name, rank, spell_level, effect1, effect_trigger_spell1,"
            " effect2, effect_misc_value2, effect_base_points2)"
            " VALUES(?,?,?,?,?,?,?,?,?)",
            [
                (133, "Fireball", "Rank 1", 1, 2, 0, 0, 0, 0),
                (143, "Fireball", "Rank 2", 6, 2, 0, 0, 0, 0),
                (116, "Frostbolt", "Rank 1", 4, 6, 0, 0, 0, 0),
                # Apprentice Herbalist wrapper: learn 2366 and set skill 182 step 1.
                (2372, "Apprentice Herbalist", "", 0, 36, 2366, 44, 182, 0),
                (2366, "Herb Gathering", "Apprentice", 0, 33, 0, 118, 182, 0),
            ],
        )
        con.execute(
            "INSERT OR REPLACE INTO creature_templates"
            "(entry, name, npc_flags, trainer_type, trainer_class,"
            " trainer_template_id, min_level, max_level)"
            " VALUES(198, 'Khelden Bremen', 17, 0, 4, 0, 1, 1)")
        con.execute(
            "INSERT OR REPLACE INTO creature_spawns"
            "(guid, entry, map, x, y, z) VALUES(1, 198, 0, 0, 0, 0)")
        con.executemany(
            "INSERT OR REPLACE INTO npc_trainer"
            "(entry, spell, spellcost, reqlevel) VALUES(?,?,?,?)",
            [(198, 133, 100, 1), (198, 143, 500, 6), (198, 116, 100, 4)])
        con.execute(
            "INSERT OR REPLACE INTO trainer_greeting(entry, text)"
            " VALUES(198, 'Ready for training?')")
        con.execute(
            "INSERT OR REPLACE INTO gossip_menus(menu_id, text_id)"
            " VALUES(4659, 0)")
        con.execute(
            "INSERT OR REPLACE INTO gossip_options"
            "(menu_id, option_index, option_type, icon, coded, box_money,"
            " text, box_text, action_menu_id, action_text_id)"
            " VALUES(4659, 0, 5, 3, 0, 0, 'Train me.', '', 0, 0)")
        con.commit()
    finally:
        con.close()
    return db


def test_trainer_list_states_and_requirements(tmp_path):
    db = _world_db(tmp_path)
    spawn = creature.get_creature_spawn(db, 1)
    assert spawn is not None
    mage = {"class": 4, "race": 1, "level": 5}
    warrior = {"class": 1, "race": 1, "level": 5}
    assert trainer.is_trainer_of(db, spawn, mage, ())
    assert not trainer.is_trainer_of(db, spawn, warrior, ())

    entries = trainer.get_trainer_spells(db, 198)
    assert [entry.spell for entry in entries] == [116, 133, 143]
    fireball = next(entry for entry in entries if entry.spell == 133)
    fireball2 = next(entry for entry in entries if entry.spell == 143)
    assert trainer.spell_state(db, fireball, mage, set(), {}) \
        == trainer.TRAINER_SPELL_GREEN
    # Rank 2 needs the level-6 spell level and rank 1 in the spellbook.
    assert trainer.spell_state(db, fireball2, mage, set(), {}) \
        == trainer.TRAINER_SPELL_RED
    assert trainer.spell_state(db, fireball2, {**mage, "level": 10}, {133}, {}) \
        == trainer.TRAINER_SPELL_GREEN
    assert trainer.spell_state(db, fireball, mage, {133}, {}) \
        == trainer.TRAINER_SPELL_GRAY

    payload, count = trainer.build_trainer_list(
        db, 1, spawn, mage, set(), {})
    assert count == 3
    guid, trainer_type, packet_count = struct.unpack_from("<QII", payload, 0)
    assert guid == 1 and trainer_type == 0 and packet_count == 3
    offset = 16
    first_spell, state = struct.unpack_from("<IB", payload, offset)
    assert first_spell == 116 and state == trainer.TRAINER_SPELL_GREEN
    assert payload.endswith(b"Ready for training?\x00")


def test_profession_wrapper_teaches_rank_and_skill(tmp_path):
    db = _world_db(tmp_path)
    # Append the profession row directly and read it back.
    con = sqlite3.connect(db)
    try:
        con.execute(
            "INSERT INTO npc_trainer(entry, spell, spellcost, reqlevel)"
            " VALUES(198, 2372, 10, 0)")
        con.commit()
    finally:
        con.close()
    trainer.clear_caches()
    entries = trainer.get_trainer_spells(db, 198)
    wrapper = next(entry for entry in entries if entry.spell == 2372)
    assert wrapper.is_castable
    assert wrapper.learned == (2372, 2366)
    assert wrapper.primary_profession_first_rank
    assert trainer.learn_spells_for(wrapper) == (2366,)

    template = spells.get_spell_template(db, 2372)
    assert trainer.skill_step_from_template(template) == (182, 1)


def test_previous_rank_uses_the_cmangos_rank_string(tmp_path):
    db = _world_db(tmp_path)
    assert trainer.previous_rank(db, 133) == 0
    assert trainer.previous_rank(db, 143) == 133


def test_character_skill_fields_do_not_overwrite_languages(tmp_path):
    skill_id = 182
    slots = object_update.language_skill_count({"race": 1})
    char = {
        "race": 1,
        "skills": {
            skill_id: {"slot": slots + 1, "value": 1, "max_value": 75,
                       "step": 1},
        },
    }
    object_update.assign_skill_slots(char)
    assert char["skills"][skill_id]["slot"] == slots + 1
    fields = object_update.skill_field_values(char, skill_id)
    base = 636 + (slots + 1) * 3
    assert fields[base] == skill_id | (1 << 16)
    assert fields[base + 1] == 1 | (75 << 16)


class _TrainerSession(TrainersMixin):
    def __init__(self, db: str, char: dict):
        self.config = types.SimpleNamespace(
            database=types.SimpleNamespace(world_path=db))
        self.in_world = True
        self.character = char
        self.player_guid = 1
        self.known_spell_ids = set()
        self.sent = []
        self.player_fields = {}
        self.log = types.SimpleNamespace(info=lambda *a: None,
                                         debug=lambda *a: None)

    async def send_packet(self, opcode, payload=b""):
        self.sent.append((opcode, payload))

    async def _send_player_fields(self, fields):
        self.player_fields.update(fields)

    async def _modify_money(self, delta):
        self.character["money"] += int(delta)

    def _get_creature_spawn(self, low_guid):
        return creature.get_creature_spawn(
            self.config.database.world_path, int(low_guid))

    def _player_position(self):
        return (0.0, 0.0, 0.0)

    def _creature_position(self, low_guid):
        return (0.0, 0.0, 0.0)

    @staticmethod
    def _distance(first, second):
        return 0.0


def test_buy_spell_learns_and_supersedes_rank(tmp_path):
    db = _world_db(tmp_path)
    char = {"name": "Tester", "class": 4, "race": 1, "level": 10,
            "money": 1000, "skills": {}}
    session = _TrainerSession(db, char)
    guid = creature.creature_guid(1, 198)

    async def run():
        # Rank 1 first.
        await session.handle_trainer_buy_spell(
            struct.pack("<QI", guid, 133))
        assert 133 in session.known_spell_ids
        assert char["money"] == 900
        assert any(opcode == Opcodes.SMSG_TRAINER_BUY_SUCCEEDED
                   for opcode, _ in session.sent)
        # Rank 2 supersedes rank 1.
        await session.handle_trainer_buy_spell(
            struct.pack("<QI", guid, 143))
        assert 143 in session.known_spell_ids
        assert 133 not in session.known_spell_ids
        assert any(opcode == Opcodes.SMSG_SUPERCEDED_SPELL
                   for opcode, _ in session.sent)

    asyncio.run(run())


def test_buy_without_money_sends_the_3_3_5_failure_packet(tmp_path):
    db = _world_db(tmp_path)
    char = {"name": "Tester", "class": 4, "race": 1, "level": 10,
            "money": 0, "skills": {}}
    session = _TrainerSession(db, char)
    guid = creature.creature_guid(1, 198)

    async def run():
        await session.handle_trainer_buy_spell(struct.pack("<QI", guid, 133))
        failure = next(
            (payload for opcode, payload in session.sent
             if opcode == Opcodes.SMSG_TRAINER_BUY_FAILED), None)
        assert failure is not None
        assert struct.unpack_from("<II", failure, 8) == (
            133, trainer.TRAINER_FAIL_NOT_ENOUGH_MONEY)
        assert 133 not in session.known_spell_ids

    asyncio.run(run())


def test_configured_start_money_is_granted(tmp_path):
    db = str(tmp_path / "world.db")
    character_db.init_character_db(db)
    character_db.configure(db, db, start_money=75)
    try:
        guid = character_db.create_character(
            db, 1, "Copperpurse", 1, 4, 0, 0, 0, 0, 0, 0)
        con = sqlite3.connect(db)
        try:
            assert con.execute(
                "SELECT money FROM characters WHERE guid = ?", (guid,)
            ).fetchone()[0] == 75
        finally:
            con.close()
    finally:
        character_db.configure(db, db, start_money=0)


class _GossipSession(CharactersMixin):
    def __init__(self, db: str, spawn):
        self.config = types.SimpleNamespace(
            database=types.SimpleNamespace(world_path=db))
        self.in_world = True
        self.character = {"name": "Tester", "class": 4, "race": 1, "level": 2}
        self.known_spell_ids = set()
        self.sent = []
        self._spawn = spawn
        self.log = types.SimpleNamespace(info=lambda *a: None,
                                         debug=lambda *a: None)

    async def send_packet(self, opcode, payload=b""):
        self.sent.append((opcode, payload))

    def _trainer_spawn(self, guid):
        return self._spawn

    def _can_use_trainer(self, spawn):
        return True

    async def _send_trainer_list(self, guid, spawn):
        self.sent.append(("trainer-list", guid))


def test_trainer_gossip_select_opens_list_without_gossip_complete(tmp_path):
    """The 3.3.5 client drops the interaction if GOSSIP_COMPLETE comes first."""
    db = _world_db(tmp_path)
    spawn = creature.get_creature_spawn(db, 1)
    session = _GossipSession(db, spawn)
    guid = creature.creature_guid(1, 198)

    asyncio.run(session.handle_gossip_select_option(
        struct.pack("<QII", guid, 4659, 0)))
    assert session.sent == [("trainer-list", guid)]


def test_buy_profession_wrapper_sets_skill(tmp_path):
    db = _world_db(tmp_path)
    con = sqlite3.connect(db)
    try:
        con.execute(
            "INSERT INTO npc_trainer(entry, spell, spellcost, reqlevel)"
            " VALUES(198, 2372, 10, 0)")
        con.commit()
    finally:
        con.close()
    char = {"name": "Tester", "class": 4, "race": 1, "level": 10,
            "money": 1000, "skills": {}}
    session = _TrainerSession(db, char)
    guid = creature.creature_guid(1, 198)

    async def run():
        await session.handle_trainer_buy_spell(struct.pack("<QI", guid, 2372))
        assert 2366 in session.known_spell_ids
        assert 2372 not in session.known_spell_ids
        assert char["skills"][182]["value"] == 1
        assert char["skills"][182]["max_value"] == 75
        assert char["skills"][182]["step"] == 1
        assert session.player_fields
        assert char["money"] == 990

    asyncio.run(run())
