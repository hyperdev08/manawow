"""Flight paths: DBC parsing, routes, packets and a real client taking a flight."""
from __future__ import annotations

import asyncio
import os
import re
import sqlite3
import struct

import pytest

from src.auth.server import AuthServer
from src.common.config import AuthConfig, Config, DBConfig, LogConfig, WorldConfig
from src.crypto.srp6 import SRP6
from src.protocol.opcodes import Opcodes
from src.world import object_update, taxi
from src.world.server import WorldServer
from src.world.worldsocket.taxi import TaxiMixin
from tools import protocol_selftest as st
from tools.init_db import init_databases

START = (-8949.95, -132.49, 83.53)                  # human warrior start


# ----------------------------------------------------------------------
# Synthetic DBCs
# ----------------------------------------------------------------------
def _write_dbc(path: str, fmt: str, rows) -> None:
    size = struct.calcsize("<" + fmt)
    fields = sum(int(count or 1) for count, _code in re.findall(r"(\d*)([A-Za-z])", fmt))
    body = b"".join(struct.pack("<" + fmt, *row) for row in rows)
    header = struct.pack("<4s4I", b"WDBC", len(rows), fields, size, 1)
    with open(path, "wb") as handle:
        handle.write(header + body + b"\x00")


def _node(node_id, map_id, pos, horde=0, alliance=0):
    return (node_id, map_id, *pos, *([0] * 17), horde, alliance)


def write_taxi_dbcs(dbc_dir: str, nodes, paths, path_nodes) -> None:
    """``nodes``: (id, map, pos, horde, alliance); ``paths``: (id, from, to, cost);
    ``path_nodes``: path id -> [pos, ...]."""
    os.makedirs(dbc_dir, exist_ok=True)
    _write_dbc(os.path.join(dbc_dir, "TaxiNodes.dbc"), "II3f17I2I",
               [_node(*n) for n in nodes])
    _write_dbc(os.path.join(dbc_dir, "TaxiPath.dbc"), "4I", list(paths))
    rows, row_id = [], 1
    for path_id, points in path_nodes.items():
        for index, pos in enumerate(points):
            rows.append((row_id, path_id, index, 0, *pos, 0, 0, 0, 0))
            row_id += 1
    _write_dbc(os.path.join(dbc_dir, "TaxiPathNode.dbc"), "4I3f4I", rows)


@pytest.fixture
def sample(tmp_path):
    write_taxi_dbcs(
        str(tmp_path),
        nodes=[(2, 0, (0.0, 0.0, 0.0), 0, 541),
               (5, 0, (300.0, 0.0, 0.0), 0, 541),
               (9, 0, (600.0, 0.0, 0.0), 3574, 0),          # horde only
               (11, 1, (1.0, 1.0, 1.0), 3574, 541)],
        paths=[(1, 2, 5, 120), (2, 5, 9, 80)],
        path_nodes={1: [(0, 0, 0), (100, 10, 20), (300, 0, 0)],
                    2: [(300, 0, 0), (450, 10, 20), (600, 0, 0)]})
    return taxi.TaxiData.load(str(tmp_path))


# ----------------------------------------------------------------------
# Unit tests
# ----------------------------------------------------------------------
def test_dbc_layout(sample):
    assert sample.nodes[2] == taxi.TaxiNode(2, 0, 0.0, 0.0, 0.0, 0, 541)
    assert sample.paths[(2, 5)].cost == 120
    assert [(n.x, n.y) for n in sample.path_nodes[1]] == [(0, 0), (100, 10), (300, 0)]


def test_bad_dbc_is_rejected(tmp_path):
    path = tmp_path / "TaxiPath.dbc"
    path.write_bytes(b"NOPE" + b"\x00" * 16)
    with pytest.raises(ValueError):
        taxi.read_dbc(str(path), "4I")


def test_nearest_node_respects_map_and_team(sample):
    assert sample.nearest_node(0, 290, 5, 0, alliance=True) == 5
    assert sample.nearest_node(0, 590, 0, 0, alliance=True) == 5      # 9 is horde only
    assert sample.nearest_node(0, 590, 0, 0, alliance=False) == 9
    assert sample.nearest_node(1, 0, 0, 0, alliance=True) == 11
    assert sample.nearest_node(530, 0, 0, 0, alliance=True) == 0


def test_route_joins_hops_and_drops_the_start_node(sample):
    route = sample.route([2, 5, 9], alliance=True)
    assert route.cost == 200
    assert route.mount_entry == 541
    assert route.points == [(100, 10, 20), (300, 0, 0), (450, 10, 20), (600, 0, 0)]
    assert sample.route([2, 9], alliance=True) is None                  # no direct path
    assert sample.route([2], alliance=True) is None
    assert sample.route([2, 77], alliance=True) is None


def test_mount_falls_back_to_the_other_team():
    node = taxi.TaxiNode(1, 0, 0, 0, 0, horde_mount=3574, alliance_mount=0)
    assert node.mount_for(alliance=True) == 3574
    assert node.mount_for(alliance=False) == 3574


def test_taxi_mask_bits():
    words = taxi.build_taxi_mask([1, 2, 32, 33, 448, 449, 0])
    assert len(words) == taxi.TAXI_MASK_SIZE
    assert words[0] == 0b11 | (1 << 31)
    assert words[1] == 1
    assert words[13] == 1 << 31
    assert sum(bin(w).count("1") for w in words) == 5                   # 449 is out of range


def test_starting_nodes():
    assert taxi.starting_nodes(1) == [2, 100]            # human: Stormwind, Honor Hold
    assert taxi.starting_nodes(4) == [26, 27, 100]
    assert taxi.starting_nodes(2, level=70) == [23, 99, 213]


def test_known_nodes_are_seeded_and_saved(tmp_path):
    db = str(tmp_path / "chars.db")
    assert taxi.load_known_nodes(db, 7, race=1, level=1) == {2, 100}
    taxi.save_known_node(db, 7, 5)
    taxi.save_known_node(db, 7, 5)
    assert taxi.load_known_nodes(db, 7, race=1, level=1) == {2, 5, 100}
    assert taxi.load_known_nodes(db, 8, race=2, level=1) == {23, 99}


def test_packets():
    known = taxi.build_show_taxi_nodes(0xF130000127000001, 2, [2, 5])
    assert len(known) == 4 + 8 + 4 + 4 * taxi.TAXI_MASK_SIZE
    one, guid, current = struct.unpack_from("<IQI", known)
    assert (one, guid, current) == (1, 0xF130000127000001, 2)
    assert struct.unpack_from("<I", known, 16)[0] == 0b10010
    assert taxi.build_taxinode_status(5, True) == struct.pack("<QB", 5, 1)
    assert taxi.build_activate_taxi_reply(3) == struct.pack("<I", 3)
    assert taxi.parse_activate_taxi(struct.pack("<QII", 9, 2, 5)) == (9, [2, 5])
    assert taxi.parse_activate_taxi_express(struct.pack("<QI3I", 9, 3, 2, 5, 9)) == (9, [2, 5, 9])
    assert taxi.parse_activate_taxi_express(struct.pack("<QI2I", 9, 3, 2, 5)) is None
    assert taxi.parse_activate_taxi(b"\x00" * 15) is None


def _parse_move(payload: bytes):
    guid, pos = st.read_packed_guid(payload, 0)
    pos += 1                                              # toggleMoveflagUnk4
    start = struct.unpack_from("<fff", payload, pos)
    move_id, mode = struct.unpack_from("<IB", payload, pos + 12)
    pos += 17
    result = {"guid": guid, "start": start, "mode": mode}
    if mode == object_update.MONSTER_MOVE_STOP:
        assert pos == len(payload)
        return result
    flags, duration, count = struct.unpack_from("<III", payload, pos)
    pos += 12
    points = [struct.unpack_from("<fff", payload, pos + 12 * i) for i in range(count)]
    assert pos + 12 * count == len(payload)
    result.update(flags=flags, duration=duration, points=points)
    return result


def test_flight_move_layout():
    payload = taxi.build_flight_move(0x42, (1, 2, 3), [(10, 2, 3), (20, 2, 3)], 1500, move_id=7)
    move = _parse_move(payload)
    assert move["guid"] == 0x42 and move["start"] == (1, 2, 3)
    assert move["flags"] == taxi.FLIGHT_SPLINE_FLAGS
    assert move["duration"] == 1500
    assert move["points"] == [(10, 2, 3), (20, 2, 3)]
    stop = _parse_move(taxi.build_move_stop(0x42, (20, 2, 3)))
    assert stop["mode"] == object_update.MONSTER_MOVE_STOP and stop["start"] == (20, 2, 3)


def test_spline_timing_and_track():
    # A straight spline has the length of the line it follows.
    assert taxi.spline_length((0, 0, 0), [(32, 0, 0), (64, 0, 0)]) == pytest.approx(64.0)
    assert taxi.flight_duration_ms((0, 0, 0), [(64, 0, 0)]) == 2000
    # A bent one is at least as long as its chord.
    assert taxi.spline_length((0, 0, 0), [(50, 50, 0), (100, 0, 0)]) > 100
    track = taxi.FlightTrack((0, 0, 0), [(10, 0, 0), (10, 10, 0)], 2000)
    assert track.position(0) == (0, 0, 0)
    assert track.position(0.5) == pytest.approx((5, 0, 0))
    assert track.position(1.5) == pytest.approx((10, 5, 0))
    assert track.position(9) == (10, 10, 0) == track.destination


# ----------------------------------------------------------------------
# End to end: a simulated 3.3.5a client discovers a node and flies
# ----------------------------------------------------------------------
MASTER = object_update.creature_guid(1003, 295)     # turned into a flight master
NEW_NODE, HOME_NODE, FAR_NODE = 7, 2, 5             # human knows node 2 already
DESTINATION = (START[0] + 64.0, START[1], START[2] + 10.0)


def _prepare(world_db: str, client_dir: str) -> None:
    master = (START[0] + 2, START[1], START[2])
    write_taxi_dbcs(
        os.path.join(client_dir, "dbc"),
        nodes=[(NEW_NODE, 0, master, 0, 541),
               (HOME_NODE, 0, DESTINATION, 0, 541),
               (FAR_NODE, 0, (START[0], START[1] + 500, START[2]), 0, 541)],
        paths=[(1, NEW_NODE, HOME_NODE, 30), (2, NEW_NODE, FAR_NODE, 10)],
        path_nodes={1: [master, (START[0] + 32, START[1], START[2] + 5), DESTINATION],
                    2: [master, (START[0], START[1] + 500, START[2])]})
    con = sqlite3.connect(world_db)
    try:
        con.execute("INSERT OR REPLACE INTO creature_templates(entry, name, display_id1) "
                    "VALUES(541, 'Riding Gryphon', 6852)")
        con.execute("UPDATE creature_templates SET npc_flags = ?, gossip_menu_id = 0 "
                    "WHERE entry = 295", (taxi.NPC_FLAG_FLIGHTMASTER | 1,))
        con.execute("UPDATE creature_spawns SET x = ?, y = ?, z = ? WHERE guid = 1003", master)
        con.commit()
    finally:
        con.close()


async def _read_until(reader, crypt, wanted, timeout=5, mover=None):
    """Read until an opcode in ``wanted``; ``mover`` also waits for a
    ``SMSG_MONSTER_MOVE`` of that guid (creatures wander meanwhile)."""
    seen = []
    while True:
        try:
            opcode, payload = await asyncio.wait_for(
                st.read_server_packet(reader, crypt, skip_monster_moves=mover is None),
                timeout)
        except TimeoutError:
            raise AssertionError(f"none of {wanted} in {[hex(op) for op, _ in seen]}")
        seen.append((opcode, payload))
        if opcode == Opcodes.SMSG_MONSTER_MOVE and mover is not None:
            if st.read_packed_guid(payload, 0)[0] == mover:
                return opcode, payload, seen
        elif opcode in wanted:
            return opcode, payload, seen


def _character(world_db: str, guid: int):
    con = sqlite3.connect(world_db)
    try:
        return con.execute("SELECT money, x, y, z FROM characters WHERE guid = ?",
                           (guid,)).fetchone()
    finally:
        con.close()


async def _flight_flow(tmp_path) -> None:
    auth_db, world_db = str(tmp_path / "auth.db"), str(tmp_path / "world.db")
    client_dir = str(tmp_path / "client")
    cfg = Config(
        authserver=AuthConfig(host="127.0.0.1", port=0, realm_id=1, realm_name="T",
                              realm_address="127.0.0.1", realm_build_min=st.BUILD,
                              realm_build_max=st.BUILD),
        worldserver=WorldConfig(host="127.0.0.1", port=0, realm_id=1, motd="T",
                                client_data=client_dir),
        database=DBConfig(auth_path=auth_db, world_path=world_db),
        logging=LogConfig(level="WARNING", log_dir=str(tmp_path / "logs"), console=False),
    )
    init_databases(cfg)
    _prepare(world_db, client_dir)
    taxi.clear_cache()
    salt, verifier = SRP6.make_registration_data(st.USERNAME, st.PASSWORD)
    con = sqlite3.connect(auth_db)
    con.execute("INSERT INTO account(username, v, s, expansion) VALUES(?,?,?,2)",
                (st.USERNAME, verifier, salt))
    con.commit()
    con.close()

    world = await asyncio.start_server(WorldServer(cfg).handle_client, "127.0.0.1", 0)
    cfg.worldserver.port = world.sockets[0].getsockname()[1]
    auth = await asyncio.start_server(AuthServer(cfg).handle_client, "127.0.0.1", 0)
    cfg.authserver.port = auth.sockets[0].getsockname()[1]
    writer = None
    try:
        session_key, _addr, realm_id = await st.authserver_flow("127.0.0.1", cfg.authserver.port)
        reader, writer, crypt = await st.world_handshake(
            "127.0.0.1", cfg.worldserver.port, st.USERNAME, session_key, realm_id)
        create = b"Flyer\x00" + bytes([1, 1, 0, 0, 0, 0, 0, 0, 0])
        await st.send_client_packet(writer, int(Opcodes.CMSG_CHAR_CREATE), create, crypt)
        await st.read_server_packet(reader, crypt)
        await st.send_client_packet(writer, int(Opcodes.CMSG_CHAR_ENUM), crypt=crypt)
        _op, enum_payload = await st.read_server_packet(reader, crypt)
        player = struct.unpack_from("<Q", enum_payload, 1)[0]
        con = sqlite3.connect(world_db)
        con.execute("UPDATE characters SET money = 100 WHERE guid = ?", (player,))
        con.commit()
        con.close()
        await st.send_client_packet(writer, int(Opcodes.CMSG_PLAYER_LOGIN),
                                    struct.pack("<Q", player), crypt)
        await st.read_login_burst(reader, crypt)

        async def send(opcode, payload):
            await st.send_client_packet(writer, int(opcode), payload, crypt)

        # -- an unknown master: status 0 ---------------------------------
        await send(Opcodes.CMSG_TAXINODE_STATUS_QUERY, struct.pack("<Q", MASTER))
        _op, payload, _ = await _read_until(reader, crypt, {Opcodes.SMSG_TAXINODE_STATUS})
        assert payload == taxi.build_taxinode_status(MASTER, False)

        # -- talking to it discovers the node and opens the map ----------
        await send(Opcodes.CMSG_GOSSIP_HELLO, struct.pack("<Q", MASTER))
        _op, payload, seen = await _read_until(reader, crypt, {Opcodes.SMSG_SHOWTAXINODES})
        opcodes = [op for op, _ in seen]
        assert Opcodes.SMSG_NEW_TAXI_PATH in opcodes
        assert Opcodes.SMSG_GOSSIP_MESSAGE not in opcodes
        assert payload == taxi.build_show_taxi_nodes(MASTER, NEW_NODE, {NEW_NODE, HOME_NODE, 100})

        # -- a second visit: known now, the map opens again --------------
        await send(Opcodes.CMSG_TAXIQUERYAVAILABLENODES, struct.pack("<Q", MASTER))
        _op, payload, seen = await _read_until(reader, crypt, {Opcodes.SMSG_SHOWTAXINODES})
        assert Opcodes.SMSG_NEW_TAXI_PATH not in [op for op, _ in seen]

        async def activate(source, destination):
            await send(Opcodes.CMSG_ACTIVATETAXI, struct.pack("<QII", MASTER, source, destination))
            _op, reply, seen = await _read_until(reader, crypt, {Opcodes.SMSG_ACTIVATETAXIREPLY})
            return struct.unpack("<I", reply)[0], seen

        # -- errors ------------------------------------------------------
        assert (await activate(NEW_NODE, FAR_NODE))[0] == taxi.ERR_TAXINOTVISITED
        assert (await activate(HOME_NODE, NEW_NODE))[0] == taxi.ERR_TAXIUNSPECIFIEDSERVERERROR
        assert (await activate(NEW_NODE, NEW_NODE))[0] == taxi.ERR_TAXISAMENODE
        assert _character(world_db, player)[0] == 100

        # -- the flight: 30c, mounted, smooth spline, lands at the node --
        error, _ = await activate(NEW_NODE, HOME_NODE)
        assert error == taxi.ERR_TAXIOK
        _op, payload, seen = await _read_until(reader, crypt, set(), mover=player)
        move = _parse_move(payload)
        assert move["guid"] == player
        assert move["flags"] == taxi.FLIGHT_SPLINE_FLAGS
        assert move["points"][-1] == pytest.approx(DESTINATION, abs=1e-3)
        assert 1500 < move["duration"] < 3500
        assert _character(world_db, player)[0] == 70

        _op, payload, _ = await _read_until(reader, crypt, set(), mover=player,
                                            timeout=move["duration"] / 1000 + 5)
        assert _parse_move(payload)["mode"] == object_update.MONSTER_MOVE_STOP
        # Ping so the landing's database write is done before reading it.
        await send(Opcodes.CMSG_PING, struct.pack("<II", 1, 0))
        await _read_until(reader, crypt, {Opcodes.SMSG_PONG})
        _money, x, y, z = _character(world_db, player)
        assert (x, y, z) == pytest.approx(DESTINATION, abs=1e-3)
    finally:
        if writer is not None:
            writer.close()
        world.close()
        auth.close()
        await world.wait_closed()
        await auth.wait_closed()
        taxi.clear_cache()


def test_flight_end_to_end(tmp_path):
    asyncio.run(_flight_flow(tmp_path))


# ----------------------------------------------------------------------
# Logging out in the air
# ----------------------------------------------------------------------
class _Base:
    def __init__(self):
        self.saved = []
        self.character = {"x": 0.0, "y": 0.0, "z": 50.0}

    async def handle_logout_request(self, data):
        self.saved.append((self.character["x"], self.character["y"], self.character["z"]))
        self.character = None

    def stop_combat_loop(self):
        self.saved.append((self.character["x"], self.character["y"], self.character["z"]))


class _Session(TaxiMixin, _Base):
    pass


async def _airborne(session):
    track = taxi.FlightTrack((0, 0, 50), [(100, 0, 60), (200, 0, 5)], 60000)
    task = asyncio.create_task(asyncio.sleep(60))
    session._taxi_flight = {"track": track, "task": task, "started": 0.0}
    session._taxi_known_nodes = {2, 5}
    return task


def test_logout_in_the_air_saves_the_destination():
    async def run():
        session = _Session()
        task = await _airborne(session)
        await session.handle_logout_request(b"")
        await asyncio.sleep(0)
        assert session.saved == [(200, 0, 5)]
        assert task.cancelled()
        assert session._taxi_flight is None
        assert session._taxi_known_nodes is None      # not inherited by the next character
    asyncio.run(run())


def test_disconnect_in_the_air_saves_the_destination():
    async def run():
        session = _Session()
        task = await _airborne(session)
        session.stop_combat_loop()
        await asyncio.sleep(0)
        assert session.saved == [(200, 0, 5)]
        assert task.cancelled()
    asyncio.run(run())
