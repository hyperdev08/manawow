"""Spawns stored with id 0 take their entry from spawn-entry lists and spawn groups."""
from __future__ import annotations

from tools import import_wotlk_db as importer


def _spawn(guid, entry=0, **kw):
    row = {"guid": guid, "id": entry, "map": 0, "spawnMask": 1, "phaseMask": 1,
           "position_x": 1.0, "position_y": 2.0, "position_z": 3.0, "orientation": 0.5,
           "spawntimesecsmin": 300, "MovementType": 1, "spawndist": 5.0}
    row.update(kw)
    return row


GROUPS = {
    "spawn_group": [{"Id": "7", "Type": "0"}, {"Id": "8", "Type": "1"}],
    "spawn_group_spawn": [{"Id": "7", "Guid": "3"}, {"Id": "7", "Guid": "4"},
                          {"Id": "8", "Guid": "3"}],
    "spawn_group_entry": [{"Id": "7", "Entry": "285", "Chance": "0"},      # Murloc
                          {"Id": "7", "Entry": "732", "Chance": "0"},      # Murloc Lurker
                          {"Id": "8", "Entry": "1617", "Chance": "0"}],
}


def test_entries_from_spawn_entry_lists_and_groups():
    spawn_entry = [{"guid": "1", "entry": "500"}, {"guid": "1", "entry": "501"},
                   {"guid": "2", "entry": "999"},                         # unknown entry
                   {"guid": "3", "entry": "502"}]                          # list wins
    known = {285, 500, 501, 502, 732}
    entries = importer.resolve_spawn_entries(spawn_entry, GROUPS,
                                             importer.SPAWN_GROUP_CREATURE, known)
    assert entries[1] in (500, 501)
    assert 2 not in entries
    assert entries[3] == 502
    assert entries[4] in (285, 732)
    # Reproducible: the same guid always gets the same entry.
    again = importer.resolve_spawn_entries(spawn_entry, GROUPS,
                                           importer.SPAWN_GROUP_CREATURE, known)
    assert again == entries
    objects = importer.resolve_spawn_entries(None, GROUPS, importer.SPAWN_GROUP_GAMEOBJECT,
                                             {1617})
    assert objects == {3: 1617}


def test_pick_entry_follows_explicit_chances():
    assert {importer._pick_entry(g, [(1, 100), (2, 0)]) for g in range(50)} == {1}
    assert {importer._pick_entry(g, [(1, 0), (2, 0)]) for g in range(200)} == {1, 2}


def test_build_creature_spawn_rows():
    raw = [_spawn(10, 299), _spawn(11), _spawn(12), _spawn(13, 299, phaseMask=2),
           _spawn(14, 299, spawnMask=2)]
    rows, skipped = importer.build_creature_spawn_rows(raw, {299, 285}, {11: 285})
    assert [(r[0], r[1]) for r in rows] == [(10, 299), (11, 285)]
    assert skipped == {"missing_template": 1, "phase": 1, "spawn_mask": 1}
    assert rows[1][2:] == (0, 0, 1.0, 2.0, 3.0, 0.5, 0, 300, 1, 5.0)
    only_zero, _ = importer.build_creature_spawn_rows(raw, {299, 285}, {11: 285},
                                                      only_zero_id=True)
    assert [r[0] for r in only_zero] == [11]


def test_movement_rows_only_for_kept_spawns():
    raw = [{"Id": "11", "Point": "1", "PositionX": "1", "PositionY": "2", "PositionZ": "3",
            "Orientation": "0", "WaitTime": "0", "ScriptId": "0", "Comment": "x"},
           {"Id": "99", "Point": "1"}]
    rows = importer.build_creature_movement_rows(raw, {11})
    assert rows == [(11, 1, 1.0, 2.0, 3.0, 0.0, 0, 0, "x")]
