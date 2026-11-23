"""World-shared creature aggro/threat/target state."""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src.world.world_combat import WorldCombatManager


class FakeSession:
    def __init__(self, guid: int, map_id: int = 0):
        self.player_guid = guid
        self.in_world = True
        self.character = {"map": map_id, "health": 100}
        self.combat = {"dead": False, "aggro": {}, "idle": {},
                       "idle_settle": {}, "creature_auras": {}}

    def _get_combat_state(self):
        return self.combat

    def _attach_shared_creature(self, state):
        low = int(state["guid"])
        self.combat["aggro"][low] = state
        self.combat["creature_auras"][low] = state.setdefault("auras", [])

    def _forget_shared_creature(self, low_guid):
        self.combat["aggro"].pop(int(low_guid), None)
        self.combat["creature_auras"].pop(int(low_guid), None)


class FakeServer:
    def __init__(self, *sessions):
        self.sessions = list(sessions)
        self.creature_combat = WorldCombatManager(self)


def _spawn(map_id=0):
    return {"guid": 77, "entry": 1234, "map": map_id, "x": 0.0,
            "y": 0.0, "z": 0.0}


def test_shared_state_and_threat_target_switching():
    first = FakeSession(100)
    second = FakeSession(200)
    server = FakeServer(first, second)
    manager = server.creature_combat

    state = manager.engage(77, _spawn(), first.player_guid)
    assert first.combat["aggro"][77] is state
    assert state["target_guid"] == 100
    assert state["threat_table"] == {100: 0.0}

    # A second attacker joins without stealing the target at equal threat.
    assert manager.engage(77, _spawn(), second.player_guid) is state
    assert second.combat["aggro"][77] is state
    assert state["target_guid"] == 100

    manager.add_threat(77, 100, 100.0)
    manager.add_threat(77, 200, 105.0)
    assert state["target_guid"] == 100       # below the 110% switch point

    manager.add_threat(77, 200, 10.0)
    assert state["target_guid"] == 200
    assert second.combat["aggro"][77] is state

    manager.taunt(77, 100, duration=5.0)
    assert state["target_guid"] == 100
    state["taunt_until"] = 0.0
    manager.select_target(state)
    assert state["target_guid"] == 200


def test_ranged_threat_and_healing_threat_rules():
    first = FakeSession(100)
    second = FakeSession(200)
    server = FakeServer(first, second)
    manager = server.creature_combat
    state = manager.engage(77, _spawn(), first.player_guid)
    manager.add_threat(77, 100, 100.0, kind="melee")
    manager.add_threat(77, 200, 120.0, kind="ranged")
    assert state["target_guid"] == 100       # ranged switch needs 130%
    manager.add_threat(77, 200, 15.0, kind="ranged")
    assert state["target_guid"] == 200

    # Healing threat goes to the healer and can pull the mob.
    manager.add_threat(77, 100, 1000.0, kind="melee")
    manager.healing_threat(200, 100, 100.0)
    table = state["threat_table"]
    assert table[200] > 0.0


def test_shared_aura_list_and_idle_driver():
    first = FakeSession(100)
    second = FakeSession(200)
    server = FakeServer(first, second)
    manager = server.creature_combat
    state = manager.engage(77, _spawn(), first.player_guid)
    assert first.combat["aggro"][77] is state
    assert first.combat["creature_auras"][77] is state["auras"]

    idle_a = manager.idle_state(77, lambda: {"kind": "wander"})
    idle_b = manager.idle_state(77, lambda: {"kind": "should not replace"})
    assert idle_a is idle_b
    now = 100.0
    assert manager.claim_idle(77, 100, now)
    assert not manager.claim_idle(77, 200, now + 0.1)
    assert manager.claim_idle(77, 200, now + 2.0)
    manager.forget_idle(77)
    assert manager.idle_state(77, lambda: {"kind": "new"})["kind"] == "new"


def test_dead_or_logged_out_target_is_replaced():
    first = FakeSession(100)
    second = FakeSession(200)
    server = FakeServer(first, second)
    manager = server.creature_combat
    state = manager.engage(77, _spawn(), first.player_guid)
    manager.add_threat(77, 100, 100.0)
    manager.add_threat(77, 200, 50.0)
    assert state["target_guid"] == 100

    first.combat["dead"] = True
    manager.select_target(state)
    assert state["target_guid"] == 200
    assert second.combat["aggro"][77] is state

    manager.detach_session(200)
    assert state["target_guid"] == 0
    assert manager.get(77) is None
    assert 77 not in first.combat["aggro"]
    assert 77 not in second.combat["aggro"]


def test_forget_clears_every_session_view():
    first = FakeSession(100)
    second = FakeSession(200)
    server = FakeServer(first, second)
    manager = server.creature_combat
    manager.engage(77, _spawn(), first.player_guid)
    manager.engage(77, _spawn(), second.player_guid)
    assert 77 in first.combat["aggro"]
    assert 77 in second.combat["aggro"]

    manager.forget(77)
    assert manager.get(77) is None
    assert 77 not in first.combat["aggro"]
    assert 77 not in second.combat["aggro"]
