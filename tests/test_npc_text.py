"""Gossip texts come from npc_text and npc_text_broadcast_text."""
from __future__ import annotations

import struct

from src.world import login_packets
from tools import import_wotlk_db as importer


def test_broadcast_texts_fill_ids_missing_from_npc_text():
    npc_text = [{"ID": 1, "text0_0": "Hello", "text0_1": "", "lang0": 0}]
    npc_text_broadcast = [
        {"Id": 1, "BroadcastTextId0": 50},           # npc_text already has it
        {"Id": 7778, "BroadcastTextId0": 99, "BroadcastTextId1": 10753},
        {"Id": 9000, "BroadcastTextId0": 404},       # broadcast text missing
    ]
    broadcast = [
        {"Id": 50, "Text": "ignored"},
        {"Id": 99, "Text": "", "Text1": ""},         # empty: try the next slot
        {"Id": 10753, "Text": "", "Text1": "Ah, $gpriest:priestess;...",
         "LanguageID": 7, "EmoteID1": 1, "EmoteDelay1": 0},
    ]
    rows = {row[0]: row for row in importer.build_npc_text_rows(
        npc_text, npc_text_broadcast, broadcast)}

    assert set(rows) == {1, 7778}
    assert rows[1][1] == "Hello"
    text_id, male, female, language, probability, emote1, delay1 = rows[7778][:7]
    assert male == female == "Ah, $gpriest:priestess;..."
    assert (language, probability, emote1, delay1) == (7, 1, 1, 0)


def test_unknown_npc_text_still_gets_a_full_record():
    # A bare "not found" id leaves the gossip window waiting forever.
    packet = login_packets.build_npc_text_update(7778, None)
    assert struct.unpack_from("<I", packet, 0)[0] == 7778
    assert struct.unpack_from("<f", packet, 4)[0] == 1.0
    assert b"Greetings, $N.\x00Greetings, $N.\x00" in packet
    assert len(packet) > 8 * 4
