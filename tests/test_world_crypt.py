"""RC4 / world header encryption."""
from __future__ import annotations

import os

import pytest

from src.crypto.world_crypt import ARC4, WorldCrypt


def test_arc4_rfc6229_vector():
    # RFC 6229, 40-bit key 0x0102030405, keystream at offset 0
    ks = ARC4(bytes.fromhex("0102030405")).process(bytes(16))
    assert ks.hex() == "b2396305f03dc027ccc3524a0a1118a8"


def test_arc4_is_symmetric():
    key = b"secret"
    msg = b"hello azeroth"
    assert ARC4(key).process(ARC4(key).process(msg)) == msg


def test_arc4_empty_key():
    with pytest.raises(ValueError):
        ARC4(b"")


def test_server_and_client_agree_both_directions():
    key = os.urandom(40)
    server = WorldCrypt(key)
    client = WorldCrypt(key, client_side=True)

    for _ in range(5):  # stream state must stay in sync across packets
        s2c = os.urandom(4)
        c2s = os.urandom(6)
        assert client.decrypt(server.encrypt(s2c)) == s2c
        assert server.decrypt(client.encrypt(c2s)) == c2s


def test_encryption_actually_changes_bytes():
    header = bytes.fromhex("000a1234abcd")
    assert WorldCrypt(os.urandom(40)).encrypt(header) != header


def test_session_key_must_be_40_bytes():
    with pytest.raises(ValueError):
        WorldCrypt(bytes(32))
