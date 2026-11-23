"""Quest rules, packet builders and the accept -> kill -> turn-in flow."""
from __future__ import annotations

import asyncio
import os
import shutil
import sqlite3
import struct
import tempfile
import types

import pytest

from src.protocol.opcodes import Opcodes
from src.world import character_db
from src.world import creature
from src.world import quests as Q
from src.world import update_fields as F
from src.world.worldsocket.quests import QuestsMixin

GIVER_ENTRY = 197          # Marshal McBride
GIVER_LOW_GUID = 5001
WOLF_ENTRY = 299
PELT_ITEM = 50432
REWARD_A, REWARD_B = 80, 2224


def _quest_row(entry, **values):
    row = {column: 0 for column in Q.QUEST_COLUMNS}
    for name in (Q.column_name(c) for c in Q.TEXT_COLUMNS):
        row[name] = ""
    row.update(entry=entry, method=2, min_level=1, quest_level=2,
               title=f"Quest {entry}", details="Details", objectives="Objectives")
    row.update(values)
    return row


QUESTS = [
    # Talk-to quest: no objectives, completes on accept.
    _quest_row(783, title="A Threat Within", quest_level=1, rew_money_max_level=30),
    # Kill quest unlocked by 783.
    _quest_row(7, title="Kobold Camp Cleanup", prev_quest_id=783,
               req_creature_or_go_id1=WOLF_ENTRY, req_creature_or_go_count1=2,
               rew_or_req_money=25, rew_money_max_level=90),
    # Collect quest with a reward choice.
    _quest_row(33, title="Wolves Across the Border", req_item_id1=PELT_ITEM,
               req_item_count1=2, rew_choice_item_id1=REWARD_A,
               rew_choice_item_count1=1, rew_choice_item_id2=REWARD_B,
               rew_choice_item_count2=1, rew_money_max_level=90),
    # Dwarf-only and level-10 quests.
    _quest_row(400, title="Dwarves only", required_races=1 << 2),
    _quest_row(401, title="Later", min_level=5, quest_level=6),
]


@pytest.fixture(scope="module")
def _template_db():
    """Build the schema + quest rows once; each test gets a fresh copy."""
    temp_dir = tempfile.mkdtemp(prefix="manawow-quests-")
    path = os.path.join(temp_dir, "template.db")
    creature.init_creature_db(path)
    character_db.init_character_db(path)
    Q.init_quest_db(path)
    con = sqlite3.connect(path)
    try:
        columns = Q.QUEST_COLUMNS
        con.executemany(
            f"INSERT INTO quest_templates({', '.join(columns)}) "
            f"VALUES({', '.join('?' for _ in columns)})",
            [[row[c] for c in columns] for row in QUESTS],
        )
        for quest in (783, 7, 33, 400, 401):
            con.execute("INSERT INTO creature_quest_starters VALUES(?, ?)",
                        (GIVER_ENTRY, quest))
            con.execute("INSERT INTO creature_quest_enders VALUES(?, ?)",
                        (GIVER_ENTRY, quest))
        for entry, stack in ((PELT_ITEM, 20), (REWARD_A, 1), (REWARD_B, 1)):
            con.execute(
                "INSERT OR REPLACE INTO item_templates(entry, name1, stackable, "
                "display_id) VALUES(?, ?, ?, ?)", (entry, f"Item {entry}", stack, 100 + entry))
        con.execute(
            "INSERT INTO characters(guid, account_id, name, race, class, level, money) "
            "VALUES(42, 1, 'Questor', 1, 1, 1, 0)")
        con.commit()
    finally:
        con.close()
    return temp_dir, path


@pytest.fixture()
def world_db(_template_db, request):
    temp_dir, template = _template_db
    path = os.path.join(temp_dir, f"{request.node.name}.db")
    shutil.copyfile(template, path)
    Q.clear_cache()
    yield path
    Q.clear_cache()


def _state(path, **kwargs):
    return Q.QuestState(kwargs.pop("race", 1), kwargs.pop("char_class", 1),
                        kwargs.pop("level", 1), **kwargs)


# ---------------------------------------------------------------------------
# Pure rules
# ---------------------------------------------------------------------------

def test_column_names_follow_snake_case():
    assert Q.column_name("ReqCreatureOrGOId1") == "req_creature_or_go_id1"
    assert Q.column_name("RewXPId") == "rew_xp_id"
    assert Q.column_name("ZoneOrSort") == "zone_or_sort"
    assert Q.column_name("entry") == "entry"
    assert len(set(Q.QUEST_COLUMNS)) == len(Q.QUEST_COLUMNS)


def test_init_quest_db_is_idempotent(world_db):
    Q.init_quest_db(world_db)
    Q.init_quest_db(world_db)
    assert Q.get_quest_template(world_db, 7)["title"] == "Kobold Camp Cleanup"


def test_prerequisite_race_and_level_rules(world_db):
    state = _state(world_db)
    take = lambda q, s=state: Q.can_take_quest(world_db, Q.get_quest_template(world_db, q), s)
    assert take(783)
    assert not take(7), "needs 783 rewarded first"
    assert not take(400), "dwarf-only quest"
    assert not take(401), "minimum level 5"
    assert take(7, _state(world_db, rewarded={783}))
    assert take(400, _state(world_db, race=3))
    assert not take(783, _state(world_db, rewarded={783})), "not repeatable"


def test_giver_status_priorities(world_db):
    # Available "!" while a quest can be taken.
    assert Q.giver_status(world_db, GIVER_ENTRY, _state(world_db)) == Q.DIALOG_STATUS_AVAILABLE
    # Only the level-5 quest left: grey "!".
    done = {783, 7, 33}
    assert Q.giver_status(world_db, GIVER_ENTRY, _state(world_db, rewarded=done)) == \
        Q.DIALOG_STATUS_UNAVAILABLE
    # An incomplete quest alone shows a grey "?"; a complete one a yellow "?".
    log = {401: {"slot": 0, "status": Q.QUEST_STATUS_INCOMPLETE, "explored": 0,
                 "counts": [0] * 4}}
    assert Q.giver_status(world_db, GIVER_ENTRY,
                          _state(world_db, level=5, rewarded=done, log=log)) == \
        Q.DIALOG_STATUS_INCOMPLETE
    log[401]["status"] = Q.QUEST_STATUS_COMPLETE
    assert Q.giver_status(world_db, GIVER_ENTRY,
                          _state(world_db, level=5, rewarded=done, log=log)) == \
        Q.DIALOG_STATUS_REWARD


def test_quest_xp_from_max_level_money():
    quest = {"rew_money_max_level": 90, "quest_level": 2}
    assert Q.quest_xp(quest, 1) == 150
    assert Q.quest_xp(quest, 8) == 120          # 6 levels above: 80%
    assert Q.quest_xp(quest, 30) == 15          # far above: 10%
    assert Q.quest_xp({"rew_money_max_level": 0, "quest_level": 2}, 1) == 0


def test_objectives_complete_checks_kills_items_and_exploration():
    quest = {"req_creature_or_go_id1": 6, "req_creature_or_go_count1": 2,
             "req_item_id1": 10, "req_item_count1": 3, "special_flags": 2}
    entry = {"counts": [2, 0, 0, 0], "explored": 0}
    assert not Q.objectives_complete(quest, entry, {10: 3})
    entry["explored"] = 1
    assert Q.objectives_complete(quest, entry, {10: 3})
    assert not Q.objectives_complete(quest, entry, {10: 2})
    entry["counts"][0] = 1
    assert not Q.objectives_complete(quest, entry, {10: 3})


def test_slot_fields_pack_counters_and_state():
    entry = {"slot": 2, "status": Q.QUEST_STATUS_COMPLETE, "explored": 0,
             "counts": [3, 1, 0, 7]}
    fields = Q.slot_fields(2, 33, entry)
    base = F.PLAYER_START + 0x0A + 2 * 5
    assert fields[base] == 33
    assert fields[base + 1] == Q.QUEST_STATE_COMPLETE
    assert fields[base + 2] == 3 | (1 << 16)
    assert fields[base + 3] == 7 << 16
    # The last slot ends right before PLAYER_VISIBLE_ITEM_1_ENTRYID.
    assert Q.PLAYER_QUEST_LOG_1_1 + Q.MAX_QUEST_LOG_SIZE * 5 == F.PLAYER_VISIBLE_ITEM_1_ENTRYID
    assert set(Q.slot_fields(2, 0, None).values()) == {0}


# ---------------------------------------------------------------------------
# Packet builders
# ---------------------------------------------------------------------------

def _cstr(data, pos):
    end = data.index(b"\x00", pos)
    return data[pos:end].decode(), end + 1


def test_quest_details_layout(world_db):
    quest = Q.get_quest_template(world_db, 33)
    payload = Q.build_quest_details(0xF130000000C51389, quest, 1, {REWARD_A: 7})
    guid, sharer, quest_id = struct.unpack_from("<QQI", payload, 0)
    assert (guid, sharer, quest_id) == (0xF130000000C51389, 0, 33)
    title, pos = _cstr(payload, 20)
    details, pos = _cstr(payload, pos)
    objectives, pos = _cstr(payload, pos)
    assert (title, details, objectives) == ("Wolves Across the Border", "Details", "Objectives")
    auto, flags, players, unk = struct.unpack_from("<BIIB", payload, pos)
    assert auto == 1 and unk == 0
    pos += 10
    choices = struct.unpack_from("<I", payload, pos)[0]
    assert choices == 2
    assert struct.unpack_from("<III", payload, pos + 4) == (REWARD_A, 1, 7)
    pos += 4 + choices * 12
    assert struct.unpack_from("<I", payload, pos)[0] == 0     # fixed rewards
    money, xp = struct.unpack_from("<II", payload, pos + 4)
    assert (money, xp) == (0, 150)
    # Trailer: honor(8) spell(8) title/talents/arena/unk(16) rep(60) emotes(4+32).
    assert len(payload) == pos + 4 + 8 + 8 + 8 + 16 + 60 + 4 + 32


def test_quest_query_response_has_fixed_size_numeric_block(world_db):
    quest = Q.get_quest_template(world_db, 7)
    payload = Q.build_quest_query_response(quest)
    assert struct.unpack_from("<I", payload, 0)[0] == 7
    # 13 + 1 + 3 + 2 + 7 uint32s, 8+12 reward pairs, 15 rep ints, POI (16).
    numeric = 4 * (13 + 1 + 3 + 2 + 7) + 8 * (4 + 6) + 4 * 15 + 16
    title, pos = _cstr(payload, numeric)
    assert title == "Kobold Camp Cleanup"
    for _ in range(4):               # objectives, details, end, completed
        _text, pos = _cstr(payload, pos)
    target, count = struct.unpack_from("<II", payload, pos)
    assert (target, count) == (WOLF_ENTRY, 2)
    pos += 16 * 4 + 8 * 6
    for _ in range(4):
        _text, pos = _cstr(payload, pos)
    assert pos == len(payload)


def test_status_multiple_and_add_kill_layouts():
    assert Q.build_questgiver_status_multiple([(1, 8), (2, 5)]) == (
        struct.pack("<I", 2) + struct.pack("<QB", 1, 8) + struct.pack("<QB", 2, 5))
    assert Q.build_questupdate_add_kill(7, 6, 1, 8, 99) == struct.pack("<IIIIQ", 7, 6, 1, 8, 99)


# ---------------------------------------------------------------------------
# Session flow
# ---------------------------------------------------------------------------

class FakeSession(QuestsMixin):
    def __init__(self, path):
        self.config = types.SimpleNamespace(database=types.SimpleNamespace(world_path=path))
        self.player_guid = 42
        self.in_world = True
        self.character = {"guid": 42, "name": "Questor", "race": 1, "class": 1,
                          "level": 1, "money": 0, "inventory": {}}
        self.log = types.SimpleNamespace(debug=lambda *a: None, info=lambda *a: None)
        self.sent = []
        self.fields = {}
        self.xp = []
        self.giver_guid = creature.creature_guid(GIVER_LOW_GUID, GIVER_ENTRY) \
            if hasattr(creature, "creature_guid") else 0xF130000000000000 | GIVER_LOW_GUID
        self._visible_creatures = {GIVER_LOW_GUID: self.giver_guid}
        self._load_quest_state()

    async def send_packet(self, opcode, data=b""):
        self.sent.append((int(opcode), bytes(data)))

    def opcodes(self):
        return [op for op, _ in self.sent]

    def last(self, opcode):
        return next(data for op, data in reversed(self.sent) if op == int(opcode))

    def _is_player_guid(self, guid):
        return False

    def _get_creature_spawn(self, low_guid):
        if int(low_guid) == GIVER_LOW_GUID:
            return {"entry": GIVER_ENTRY, "npc_flags": 3}
        return None

    async def _send_player_fields(self, fields):
        self.fields.update(fields)

    async def _send_inventory_failure(self, error=0):
        self.sent.append(("inventory_failure", error))

    async def _send_item_create_block(self, item):
        pass

    async def broadcast_equipment_update(self):
        pass

    @staticmethod
    def _internal_slot_to_client(slot):
        return slot - 256 + 23

    async def _award_xp(self, xp, victim_guid=0):
        self.xp.append(xp)


def _run(coro):
    return asyncio.run(coro)


def _accept(session, quest_id):
    _run(session.handle_questgiver_accept_quest(
        struct.pack("<QII", session.giver_guid, quest_id, 0)))


def test_talk_quest_completes_on_accept_and_unlocks_chain(world_db):
    session = FakeSession(world_db)
    _accept(session, 783)
    assert session._quest_log[783]["status"] == Q.QUEST_STATUS_COMPLETE
    assert int(Opcodes.SMSG_QUESTUPDATE_COMPLETE) in session.opcodes()
    base = Q.PLAYER_QUEST_LOG_1_1
    assert session.fields[base] == 783
    assert session.fields[base + 1] == Q.QUEST_STATE_COMPLETE

    session.sent.clear()
    _run(session.handle_questgiver_choose_reward(
        struct.pack("<QII", session.giver_guid, 783, 0)))
    complete = session.last(Opcodes.SMSG_QUESTGIVER_QUEST_COMPLETE)
    assert struct.unpack_from("<III", complete, 0) == (783, 50, 0)
    assert session.xp == [50]
    assert session.fields[base] == 0, "slot cleared"
    assert 783 in Q.load_rewarded_quests(world_db, 42)
    assert not Q.load_quest_log(world_db, 42)
    # Next quest in the chain is now available from the same NPC.
    assert Q.can_take_quest(world_db, Q.get_quest_template(world_db, 7), session._quest_state())


def test_kill_credit_completes_and_pays_money(world_db):
    session = FakeSession(world_db)
    session._quest_rewarded.add(783)
    _accept(session, 7)
    assert session._quest_log[7]["status"] == Q.QUEST_STATUS_INCOMPLETE

    session.sent.clear()
    _run(session._quest_on_creature_killed(0xF130000000001234, WOLF_ENTRY))
    kill = session.last(Opcodes.SMSG_QUESTUPDATE_ADD_KILL)
    assert struct.unpack_from("<IIII", kill, 0) == (7, WOLF_ENTRY, 1, 2)
    assert int(Opcodes.SMSG_QUESTUPDATE_COMPLETE) not in session.opcodes()
    _run(session._quest_on_creature_killed(0xF130000000001235, WOLF_ENTRY))
    _run(session._quest_on_creature_killed(0xF130000000001236, WOLF_ENTRY))
    assert session._quest_log[7]["counts"][0] == 2, "capped at the requirement"
    assert session._quest_log[7]["status"] == Q.QUEST_STATUS_COMPLETE
    assert Q.load_quest_log(world_db, 42)[7]["counts"][0] == 2, "persisted"

    _run(session.handle_questgiver_choose_reward(
        struct.pack("<QII", session.giver_guid, 7, 0)))
    assert session.character["money"] == 25
    assert session.fields[F.PLAYER_FIELD_COINAGE] == 25


def test_collect_quest_drops_items_and_hands_out_choice(world_db):
    session = FakeSession(world_db)
    _accept(session, 33)
    assert session._quest_needs_item(PELT_ITEM)
    assert not session._quest_needs_item(REWARD_A)

    assert _run(session._quest_give_item(PELT_ITEM, 2))
    assert session._quest_log[33]["status"] == Q.QUEST_STATUS_COMPLETE
    assert not session._quest_needs_item(PELT_ITEM)

    # Asking for the reward shows the OfferReward window.
    session.sent.clear()
    _run(session.handle_questgiver_complete_quest(
        struct.pack("<QI", session.giver_guid, 33)))
    assert int(Opcodes.SMSG_QUESTGIVER_REQUEST_ITEMS) in session.opcodes()
    _run(session.handle_questgiver_request_reward(
        struct.pack("<QI", session.giver_guid, 33)))
    assert int(Opcodes.SMSG_QUESTGIVER_OFFER_REWARD) in session.opcodes()

    # An out-of-range choice is refused.
    _run(session.handle_questgiver_choose_reward(
        struct.pack("<QII", session.giver_guid, 33, 5)))
    assert 33 in session._quest_log

    _run(session.handle_questgiver_choose_reward(
        struct.pack("<QII", session.giver_guid, 33, 1)))
    entries = sorted(int(i["item_entry"]) for i in session.character["inventory"].values())
    assert entries == [REWARD_B], "pelts taken, chosen reward given"
    assert 33 in session._quest_rewarded


def test_losing_quest_items_reopens_the_quest(world_db):
    session = FakeSession(world_db)
    _accept(session, 33)
    _run(session._quest_give_item(PELT_ITEM, 2))
    assert session._quest_log[33]["status"] == Q.QUEST_STATUS_COMPLETE
    _run(session._quest_take_items(PELT_ITEM, 1))
    _run(session._quest_items_changed([PELT_ITEM]))
    assert session._quest_log[33]["status"] == Q.QUEST_STATUS_INCOMPLETE


def test_abandon_frees_the_slot(world_db):
    session = FakeSession(world_db)
    _accept(session, 33)
    slot = session._quest_log[33]["slot"]
    _run(session.handle_questlog_remove_quest(bytes([slot])))
    assert 33 not in session._quest_log
    assert not Q.load_quest_log(world_db, 42)
    assert session.fields[Q.PLAYER_QUEST_LOG_1_1 + slot * 5] == 0


def test_refuses_quests_the_player_cannot_take(world_db):
    session = FakeSession(world_db)
    _accept(session, 7)                       # prerequisite missing
    _accept(session, 401)                     # level too low
    assert not session._quest_log
    invalid = [d for op, d in session.sent if op == int(Opcodes.SMSG_QUESTGIVER_QUEST_INVALID)]
    assert struct.unpack("<I", invalid[0])[0] == Q.INVALIDREASON_DONT_HAVE_REQ
    assert struct.unpack("<I", invalid[1])[0] == Q.INVALIDREASON_QUEST_FAILED_LOW_LEVEL


def test_quest_log_survives_relogin(world_db):
    session = FakeSession(world_db)
    _accept(session, 33)
    again = FakeSession(world_db)
    assert again._quest_log[33]["status"] == Q.QUEST_STATUS_INCOMPLETE
    assert again.character["quest_fields"][Q.PLAYER_QUEST_LOG_1_1] == 33


def test_hello_with_several_quests_sends_list(world_db):
    session = FakeSession(world_db)
    session._quest_rewarded.add(783)          # 7 and 33 both available
    _run(session.handle_questgiver_hello(struct.pack("<Q", session.giver_guid)))
    payload = session.last(Opcodes.SMSG_QUESTGIVER_QUEST_LIST)
    _greeting, pos = _cstr(payload, 8)
    count = payload[pos + 8]
    assert count == 2


def _make_auto_accept(path, quest_id):
    con = sqlite3.connect(path)
    try:
        con.execute("UPDATE quest_templates SET quest_flags = quest_flags | ? "
                    "WHERE entry = ?", (Q.QUEST_FLAGS_AUTO_ACCEPT, quest_id))
        con.commit()
    finally:
        con.close()
    Q.clear_cache()


def test_auto_accept_quest_is_added_when_details_open(world_db):
    """Northshire quests carry QUEST_FLAGS_AUTO_ACCEPT: the client's Accept
    button only closes the window, so the server must add the quest itself."""
    _make_auto_accept(world_db, 33)
    session = FakeSession(world_db)
    _run(session.handle_questgiver_query_quest(
        struct.pack("<QIB", session.giver_guid, 33, 0)))
    assert 33 in session._quest_log
    assert session.fields[Q.PLAYER_QUEST_LOG_1_1] == 33
    ops = session.opcodes()
    assert int(Opcodes.SMSG_QUESTGIVER_QUEST_DETAILS) in ops
    assert 33 in Q.load_quest_log(world_db, 42), "persisted"

    # A client that still sends ACCEPT_QUEST gets no "already on" error.
    session.sent.clear()
    _accept(session, 33)
    assert int(Opcodes.SMSG_QUESTGIVER_QUEST_INVALID) not in session.opcodes()
    assert int(Opcodes.SMSG_GOSSIP_COMPLETE) in session.opcodes()


def test_auto_accept_single_quest_hello_adds_quest(world_db):
    _make_auto_accept(world_db, 783)
    session = FakeSession(world_db)
    session._quest_rewarded.add(33)           # leave 783 as the only offer
    _run(session.handle_questgiver_hello(struct.pack("<Q", session.giver_guid)))
    assert 783 in session._quest_log
    assert int(Opcodes.SMSG_QUESTGIVER_QUEST_DETAILS) in session.opcodes()


def test_normal_quest_waits_for_accept(world_db):
    session = FakeSession(world_db)
    _run(session.handle_questgiver_query_quest(
        struct.pack("<QIB", session.giver_guid, 33, 0)))
    assert 33 not in session._quest_log


def test_status_multiple_reports_visible_givers(world_db):
    session = FakeSession(world_db)
    _run(session.handle_questgiver_status_multiple_query(b""))
    payload = session.last(Opcodes.SMSG_QUESTGIVER_STATUS_MULTIPLE)
    assert struct.unpack_from("<IQB", payload, 0) == (1, session.giver_guid,
                                                      Q.DIALOG_STATUS_AVAILABLE)


def test_inventory_stacks_only_with_same_item(world_db):
    first = character_db.add_item_to_inventory(world_db, 42, PELT_ITEM, 1)
    other = character_db.add_item_to_inventory(world_db, 42, REWARD_A, 1)
    assert first[0]["created"] and other[0]["created"]
    assert first[0]["slot"] != other[0]["slot"]
    more = character_db.add_item_to_inventory(world_db, 42, PELT_ITEM, 1)
    assert not more[0]["created"] and more[0]["slot"] == first[0]["slot"]
