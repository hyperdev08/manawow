#!/usr/bin/env python3
"""Create or update an account with valid 3.3.5a SRP6 data."""
from __future__ import annotations

import hashlib
import os
import sqlite3
import sys

root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(root, "src"))
sys.path.insert(0, root)

from src.common.config import Config
from src.crypto.srp6 import SRP6
from src.world.character_db import init_character_db
from tools.init_db import init_auth_schema


def load_config() -> Config:
    cfg_path = os.path.join(root, "config", "config.yaml")
    if not os.path.exists(cfg_path):
        cfg_path = os.path.join(root, "config", "config.example.yaml")
    return Config.load(cfg_path)


def create_account(username: str, password: str, email: str = "") -> None:
    if not username or not password:
        raise ValueError("username and password must not be empty")

    cfg = load_config()
    cfg.resolve_database_paths(root)
    os.makedirs(os.path.dirname(cfg.database.auth_path) or ".", exist_ok=True)

    user = username.upper()
    salt, verifier = SRP6.make_registration_data(user, password)
    legacy_hash = hashlib.sha1((user + ":" + password.upper()).encode("utf-8")).hexdigest().upper()

    con = sqlite3.connect(cfg.database.auth_path)
    try:
        init_auth_schema(con)
        con.execute(
            """
            INSERT INTO account(username, sha_pass_hash, v, s, email, expansion)
            VALUES(?, ?, ?, ?, ?, 2)
            ON CONFLICT(username) DO UPDATE SET
                sha_pass_hash = excluded.sha_pass_hash,
                v = excluded.v,
                s = excluded.s,
                email = excluded.email,
                expansion = excluded.expansion,
                session_key = NULL
            """,
            (user, legacy_hash, verifier, salt, email),
        )
        con.commit()
        row = con.execute("SELECT id FROM account WHERE username = ?", (user,)).fetchone()
    finally:
        con.close()

    init_character_db(cfg.database.world_path)

    print(f"Account ready: {user} (id {row[0] if row else '?'})")
    print(f"  password: {password}")
    print(f"  salt:     {salt.hex().upper()}")
    print(f"  verifier: {verifier.hex().upper()}")
    print(f"  auth db:  {cfg.database.auth_path}")


def main() -> None:
    if len(sys.argv) < 3:
        print("Usage: python tools/create_test_account.py <username> <password> [email]")
        print("Example: python tools/create_test_account.py TEST TEST")
        sys.exit(1)
    create_account(sys.argv[1], sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else "")


if __name__ == "__main__":
    main()
