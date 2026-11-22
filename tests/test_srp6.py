"""SRP6 (authserver login) — full client/server handshake in pure Python."""
from __future__ import annotations

import os

import pytest

from src.crypto.srp6 import SRP6

N, g, k = SRP6.N, SRP6.g, SRP6.k


def client_proof(username: str, password: str, salt: bytes, B_bytes: bytes):
    """What the 3.3.5a client computes after receiving (B, g, N, s)."""
    a = int.from_bytes(os.urandom(32), "little") % N or 1
    A_bytes = pow(g, a, N).to_bytes(32, "little")
    B = SRP6.from_le(B_bytes)
    u = SRP6.from_le(SRP6.h(A_bytes, B_bytes))
    x = SRP6.calculate_x(username, password, salt)
    S = pow((B - k * pow(g, x, N)) % N, a + u * x, N)
    K = SRP6.sha1_interleave(S.to_bytes(32, "little"))

    ng = bytes(p ^ q for p, q in zip(SRP6.h(SRP6.N_BYTES), SRP6.h(SRP6.G_BYTES)))
    M1 = SRP6.h(ng, SRP6.h(username.upper().encode()), salt, A_bytes, B_bytes, K)
    return A_bytes, M1, K


def test_registration_data_sizes():
    salt, verifier = SRP6.make_registration_data("test", "test")
    assert len(salt) == 32
    assert len(verifier) == 32


def test_verifier_is_case_insensitive():
    salt = bytes(range(32))
    assert SRP6.calculate_verifier("Test", "PaSs", salt) == SRP6.calculate_verifier(
        "TEST", "PASS", salt
    )


def test_full_handshake_succeeds():
    salt, verifier = SRP6.make_registration_data("TEST", "TEST")
    b, B_bytes = SRP6.generate_challenge(verifier)
    A_bytes, M1, client_key = client_proof("TEST", "TEST", salt, B_bytes)

    ok, M2, server_key = SRP6.verify_proof("TEST", A_bytes, B_bytes, b, verifier, salt, M1)

    assert ok
    assert server_key == client_key
    assert len(server_key) == SRP6.SESSION_KEY_LENGTH
    assert M2 == SRP6.h(A_bytes, M1, client_key)


def test_wrong_password_is_rejected():
    salt, verifier = SRP6.make_registration_data("TEST", "TEST")
    b, B_bytes = SRP6.generate_challenge(verifier)
    A_bytes, M1, _ = client_proof("TEST", "WRONG", salt, B_bytes)

    ok, M2, key = SRP6.verify_proof("TEST", A_bytes, B_bytes, b, verifier, salt, M1)

    assert not ok and M2 == b"" and key == b""


@pytest.mark.parametrize("A", [bytes(32), SRP6.N_BYTES])
def test_degenerate_client_key_is_rejected(A):
    """A == 0 or A == N would let an attacker force S == 0."""
    salt, verifier = SRP6.make_registration_data("TEST", "TEST")
    b, B_bytes = SRP6.generate_challenge(verifier)
    ok, _, _ = SRP6.verify_proof("TEST", A, B_bytes, b, verifier, salt, bytes(20))
    assert not ok


def test_sha1_interleave_rejects_wrong_length():
    with pytest.raises(ValueError):
        SRP6.sha1_interleave(bytes(31))
