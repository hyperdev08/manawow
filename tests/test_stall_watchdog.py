"""Stall watchdog: blocking calls are reported with their stack and duration."""
from __future__ import annotations

import argparse
import asyncio
import time

import pytest

from src.common import stall_watchdog
from src.common.stall_watchdog import StallWatchdog


def _block_the_loop(seconds: float) -> None:
    time.sleep(seconds)                 # the call the watchdog must point at


async def _run(watchdog: StallWatchdog, block: float, settle: float = 0.4) -> None:
    watchdog.start()
    await asyncio.sleep(0.1)
    if block:
        _block_the_loop(block)
    await asyncio.sleep(settle)          # let the watchdog see the loop resume
    watchdog.stop()


def test_reports_the_blocking_stack_and_duration(tmp_path):
    log = tmp_path / "logs" / "stalls.log"
    watchdog = StallWatchdog(str(log), threshold=0.2, interval=0.05)
    asyncio.run(_run(watchdog, block=0.6))
    text = log.read_text(encoding="utf-8")
    assert "loop stalled" in text
    assert "_block_the_loop" in text and "time.sleep(seconds)" in text
    assert "loop resumed after" in text
    # A slow machine can add a stall of its own (imports, loop start-up).
    assert watchdog.stalls >= 1
    assert watchdog.worst >= 0.4


def test_quiet_loop_writes_nothing(tmp_path):
    log = tmp_path / "stalls.log"
    watchdog = StallWatchdog(str(log), threshold=1.0, interval=0.05)
    asyncio.run(_run(watchdog, block=0))
    assert not log.exists()
    assert watchdog.stalls == 0 and watchdog.worst == 0.0


def test_long_stall_repeats_the_stack(tmp_path):
    log = tmp_path / "stalls.log"
    watchdog = StallWatchdog(str(log), threshold=0.1, interval=0.02, repeat=0.2)
    asyncio.run(_run(watchdog, block=0.7))
    assert log.read_text(encoding="utf-8").count("loop stalled") >= 2
    assert watchdog.stalls >= 1


def test_invalid_threshold():
    with pytest.raises(ValueError):
        StallWatchdog(threshold=0)


def test_launcher_flag(tmp_path):
    parser = argparse.ArgumentParser()
    stall_watchdog.add_argument(parser)
    assert parser.parse_args([]).watchdog is None
    assert parser.parse_args(["--watchdog"]).watchdog == stall_watchdog.DEFAULT_THRESHOLD
    assert parser.parse_args(["--watchdog", "0.25"]).watchdog == 0.25
    from src.common.config import LogConfig
    logging_config = LogConfig(level="WARNING", log_dir=str(tmp_path), console=False)
    assert stall_watchdog.start_for(None, logging_config) is None

    async def started():
        watchdog = stall_watchdog.start_for(0.5, logging_config)
        try:
            assert watchdog.log_path.endswith("stalls.log")
            assert watchdog.threshold == 0.5
        finally:
            watchdog.stop()

    asyncio.run(started())
