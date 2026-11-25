#!/usr/bin/env python3
"""Refresh the worldserver's spell data from a CMaNGOS/TrinityCore dump.

The full :mod:`import_wotlk_db` run rebuilds every NPC table, which takes a
while and replaces the hand-tuned spawn data.  This focused tool only touches
the spell side of the database:

* every ``spell_template`` row (the dump splits the table into several
  ``insert`` statements, so all of them are read),
* ``creature_template_spells`` / ``creature_spell_list`` / ``creature_cooldowns``,
* the AI columns of ``creature_templates`` (``SpellList``/``AIName``/detection),
* ``spell_rune_cost`` from the client's ``SpellRuneCost.dbc``.

The dump is streamed one insert statement at a time, so the 80k-row
``spell_template`` table does not have to be held in memory as Python dicts.

Usage::

    python tools/import_wotlk_spells.py --dump C:\\path\\WoTLKDB.sql \\
        [--db data/world.db] [--rune-cost-dbc C:\\path\\SpellRuneCost.dbc]

Stop the worldserver before running it so SQLite does not have to contend with
a live writer.
"""
from __future__ import annotations

import argparse
import os
import sys
import time

root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, root)
sys.path.insert(0, os.path.join(root, "tools"))

import wotlk_dump
import import_wotlk_db as importer


def _stream_rows(text: str, table: str):
    """Yield one dict per tuple of every insert into ``table`` (streaming)."""
    for columns, raw in wotlk_dump.iter_inserts(text, table):
        for fields in wotlk_dump.parse_tuples(raw):
            values = [wotlk_dump.convert(field) for field in fields]
            yield dict(zip(columns, values))


def _update_creature_ai(con, text: str) -> int:
    """Copy ``SpellList``/``AIName``/detection from the dump into templates."""
    sql = (
        "UPDATE creature_templates SET spell_list_id = ?, ai_name = ?,"
        " aggro_radius = ?, chase_distance = ? WHERE entry = ?"
    )
    batch = []
    count = 0
    for row in _stream_rows(text, "creature_template"):
        entry = wotlk_dump.int_field(row, "Entry")
        if not entry:
            continue
        batch.append((
            wotlk_dump.int_field(row, "SpellList"),
            wotlk_dump.str_field(row, "AIName"),
            wotlk_dump.float_field(row, "Detection", 20.0),
            wotlk_dump.float_field(row, "Pursuit", 5000.0) / 100.0,
            entry,
        ))
        count += 1
        if len(batch) >= 2000:
            con.executemany(sql, batch)
            batch.clear()
    if batch:
        con.executemany(sql, batch)
    return count


def import_spells(dump_path: str, db_path: str,
                  rune_cost_dbc: str | None = None) -> dict[str, int]:
    started = time.time()
    text = wotlk_dump.read_dump(dump_path)
    print(f"loaded {len(text):,} bytes from {dump_path}")

    # Make sure the schema (including spell_rune_cost) exists before the data
    # is replaced.  init_creature_db only seeds the demo NPCs when the spawn
    # table is empty, which it is not for a populated world database.
    from src.world.creature import init_creature_db

    init_creature_db(db_path)

    import sqlite3

    con = sqlite3.connect(db_path)
    try:
        con.execute("PRAGMA synchronous=OFF")
        for table in ("spell_templates", "creature_template_spells",
                      "creature_spell_list", "creature_spell_list_entry",
                      "creature_spell_targeting", "creature_cooldowns",
                      "spell_rune_cost"):
            con.execute(f"DELETE FROM {table}")

        counts = {
            "spells": importer._insert_mapped(
                con, "spell_templates", importer.SPELL_TEMPLATE_MAP,
                _stream_rows(text, "spell_template"),
                float_columns=importer.SPELL_FLOAT_SQLITE_COLUMNS,
                text_columns=importer.SPELL_TEXT_SQLITE_COLUMNS,
            ),
            "creature_template_spells": importer._insert_mapped(
                con, "creature_template_spells",
                importer.CREATURE_TEMPLATE_SPELLS_MAP,
                _stream_rows(text, "creature_template_spells"),
            ),
            "creature_spell_list": importer._insert_mapped(
                con, "creature_spell_list", importer.CREATURE_SPELL_LIST_MAP,
                _stream_rows(text, "creature_spell_list"),
                text_columns=frozenset(("comments",)),
            ),
            "creature_spell_list_entry": importer._insert_mapped(
                con, "creature_spell_list_entry",
                importer.CREATURE_SPELL_LIST_ENTRY_MAP,
                _stream_rows(text, "creature_spell_list_entry"),
                text_columns=frozenset(("name",)),
            ),
            "creature_spell_targeting": importer._insert_mapped(
                con, "creature_spell_targeting",
                importer.CREATURE_SPELL_TARGETING_MAP,
                _stream_rows(text, "creature_spell_targeting"),
                text_columns=frozenset(("comments",)),
            ),
            "creature_cooldowns": importer._insert_mapped(
                con, "creature_cooldowns", importer.CREATURE_COOLDOWNS_MAP,
                _stream_rows(text, "creature_cooldowns"),
            ),
            "creature_ai": _update_creature_ai(con, text),
            "rune_costs": 0,
        }

        if rune_cost_dbc:
            rune_rows = importer.read_rune_cost_dbc(rune_cost_dbc)
            counts["rune_costs"] = importer._insert_rune_costs(con, rune_rows)

        # Derive the single school index used by the client packets from the
        # DBC school mask (bit 0 = physical, 1 = holy, ... 6 = arcane).
        school_updates = [
            ((mask & -mask).bit_length() - 1, spell_id)
            for spell_id, mask in con.execute(
                "SELECT id, school_mask FROM spell_templates WHERE school_mask <> 0"
            )
        ]
        con.executemany(
            "UPDATE spell_templates SET school = ? WHERE id = ?", school_updates
        )

        con.commit()
        con.execute("PRAGMA optimize")
    finally:
        con.close()

    counts["schools"] = len(school_updates)
    print(
        f"imported {counts['spells']:,} spells, "
        f"{counts['creature_template_spells']:,} template spell sets, "
        f"{counts['creature_spell_list']:,} spell-list entries, "
        f"{counts['creature_spell_list_entry']:,} spell-list headers, "
        f"{counts['creature_spell_targeting']:,} targeting rows, "
        f"{counts['creature_cooldowns']:,} cooldowns, "
        f"{counts['creature_ai']:,} creature AI rows, "
        f"{counts['rune_costs']:,} rune costs in {time.time() - started:.1f}s"
    )
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dump",
                        help="CMaNGOS/TrinityCore world .sql (or .sql.gz) dump; "
                             "defaults to the pinned download")
    parser.add_argument("--cache-dir",
                        help="where to cache the downloaded CMaNGOS dump")
    parser.add_argument("--refresh-dump", action="store_true",
                        help="re-download the pinned CMaNGOS dump")
    parser.add_argument("--db", help="target SQLite world database "
                                     "(default: config/config.yaml)")
    parser.add_argument("--rune-cost-dbc",
                        help="path to the client's SpellRuneCost.dbc")
    args = parser.parse_args()

    from src.common.config import Config

    config_path = os.path.join(root, "config", "config.yaml")
    if not os.path.exists(config_path):
        config_path = os.path.join(root, "config", "config.example.yaml")
    config = Config.load(config_path)
    config.resolve_database_paths(root)
    db_path = args.db or config.database.world_path

    dump_path = args.dump
    if not dump_path:
        from import_wotlk_db import download_dump
        dump_path = download_dump(args.cache_dir, args.refresh_dump)

    import_spells(dump_path, db_path, rune_cost_dbc=args.rune_cost_dbc)


if __name__ == "__main__":
    main()
