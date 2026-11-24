"""Packet logging must not flush on the asyncio thread."""
from __future__ import annotations

import time
from pathlib import Path

from src.common.config import Config
from src.network.packet_log import Direction, get_packet_log


def _fresh_log():
    """Close any session another test left open, then reuse the singleton."""
    log = get_packet_log()
    log.Close()
    return log


def test_packet_log_flushes_queued_records_on_close(tmp_path):
    log = _fresh_log()
    # A deliberately huge interval proves Close (the shutdown path) is what
    # makes the records durable, not a timed flush.
    log.Initialize(str(tmp_path), enabled=True, flush_interval=60.0)
    log.LogPacket(b"\x01\x02", 0x1234, Direction.SERVER_TO_CLIENT,
                  "10.0.0.5", 4242)
    log.Close()

    files = list(tmp_path.glob("packets_*.log"))
    assert len(files) == 1
    text = files[0].read_text(encoding="utf-8")
    assert "S->C 10.0.0.5:4242" in text
    assert "0x1234" in text
    assert "01 02" in text


def test_packet_log_flushes_periodically_in_the_background(tmp_path):
    log = _fresh_log()
    log.Initialize(str(tmp_path), enabled=True, flush_interval=0.05)
    log.LogPacket(b"\xAA\xBB", 0x01, Direction.CLIENT_TO_SERVER)
    path = Path(log.path)
    deadline = time.time() + 3.0
    while time.time() < deadline:
        if path.exists() and "AA BB" in path.read_text(
                encoding="utf-8", errors="replace"):
            break
        time.sleep(0.02)
    else:
        raise AssertionError("packet log was not flushed by the writer thread")
    log.Close()


def test_packet_log_can_be_disabled(tmp_path):
    log = _fresh_log()
    log.Initialize(str(tmp_path), enabled=False)
    assert not log.CanLogPacket()
    log.LogPacket(b"\x00", 0x01, Direction.CLIENT_TO_SERVER)
    log.Close()
    assert not list(tmp_path.glob("packets_*.log"))


def test_config_can_disable_packet_logging(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "logging:\n"
        "  packet_log: false\n",
        encoding="utf-8")
    assert Config.load(config_path).logging.packet_log is False
