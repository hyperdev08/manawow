#!/usr/bin/env python3
"""Refresh NPC spell-trainer data from a CMaNGOS/TrinityCore dump.

Runs only the trainer half of the full world import: ``npc_trainer``,
``npc_trainer_template``, ``trainer_greeting`` and the ``Trainer*`` columns of
``creature_templates``.  Stop the worldserver first so SQLite does not contend
with a live writer.

Usage::

    python tools/import_wotlk_trainers.py [--dump WoTLKDB.sql|.gz] [--db data/world.db]
"""
from __future__ import annotations

import argparse
import os
import sys

root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, root)
sys.path.insert(0, os.path.join(root, "tools"))

import import_wotlk_db


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dump",
                        help="CMaNGOS/TrinityCore world .sql (or .sql.gz) dump; "
                             "defaults to the pinned download")
    parser.add_argument("--cache-dir",
                        help="where to cache the downloaded CMaNGOS dump")
    parser.add_argument("--refresh-dump", action="store_true",
                        help="re-download the pinned CMaNGOS dump")
    parser.add_argument("--db", help="target SQLite world database "
                                     "(default: config/config.yaml)")
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
        dump_path = import_wotlk_db.download_dump(
            args.cache_dir, args.refresh_dump)
    counts = import_wotlk_db.import_trainers(dump_path, db_path)
    print(
        f"imported {counts['npc_trainer']:,} trainer rows, "
        f"{counts['npc_trainer_template']:,} template rows, "
        f"{counts['trainer_greeting']:,} greetings, "
        f"{counts['creature_trainers']:,} creature trainer columns"
    )


if __name__ == "__main__":
    main()
