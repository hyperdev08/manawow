"""Packet header framing (server -> client and client -> server)."""
from __future__ import annotations

import pytest

from src.network.packet import InPacket
from src.network.packet_header import (
    ClientPacketHeader,
    ServerPacketHeader,
    parse_server_header,
)


def test_server_header_normal():
    h = ServerPacketHeader(size=0x0102, opcode=0x01EE)
    assert h.build() == bytes([0x01, 0x02, 0xEE, 0x01])
    assert h.get_header_length() == 4


def test_server_header_large_packet_uses_extra_byte():
    size = 0x012345
    h = ServerPacketHeader(size=size, opcode=0x01F6)
    raw = h.build()
    assert len(raw) == 5 == h.get_header_length()
    assert raw[0] & 0x80


@pytest.mark.parametrize("size", [2, 0x7FFF, 0x8000, 0x7FFFFF])
def test_server_header_roundtrip(size):
    raw = ServerPacketHeader(size=size, opcode=0x1234).build()
    assert parse_server_header(raw) == (len(raw), size, 0x1234)


@pytest.mark.parametrize("data", [b"", b"\x00\x04\x01", b"\x80\x00\x00\x01"])
def test_server_header_incomplete(data):
    assert parse_server_header(data) is None


def test_client_header_roundtrip():
    h = ClientPacketHeader(size=10, opcode=0x01ED)
    parsed, consumed = ClientPacketHeader.parse(h.build())
    assert consumed == 6
    assert parsed == h
    assert parsed.payload_size == 6
    assert parsed.is_valid()


@pytest.mark.parametrize("size,valid", [(3, False), (4, True), (0x8000, True), (0x8001, False)])
def test_client_header_size_limits(size, valid):
    assert ClientPacketHeader(size=size, opcode=1).is_valid() is valid


def test_client_header_incomplete():
    assert ClientPacketHeader.parse(b"\x00\x04\x00") is None


def test_inpacket_readers():
    data = bytes([7]) + (0x1234).to_bytes(2, "little") + (0xDEADBEEF).to_bytes(4, "little")
    data += b"Thrall\x00rest"
    p = InPacket(opcode=0, data=data)
    assert p.read_uint8() == 7
    assert p.read_uint16() == 0x1234
    assert p.read_uint32() == 0xDEADBEEF
    assert p.read_cstring() == "Thrall"
    assert p.read_cstring() == "rest"  # unterminated string reads to the end
