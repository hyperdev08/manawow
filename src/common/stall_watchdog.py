"""Event-loop stall watchdog.

The servers run every session on one asyncio loop, so any blocking call
(SQLite on a slow disk, a synchronous file flush, a long computation) freezes
every player at once.  The watchdog finds those calls:

* a background thread schedules a no-op on the loop every ``interval``
  seconds and checks when the loop last ran one;
* when the loop has not answered for ``threshold`` seconds, it writes the
  loop thread's current stack to ``log_path`` -- the line that is blocking --
  and again every ``repeat`` seconds while the stall lasts;
* when the loop answers again, it writes the total duration of the stall.

Usage::

    watchdog = StallWatchdog(log_path="logs/stalls.log", threshold=1.0)
    watchdog.start()        # from a coroutine running on the loop to watch
    ...
    watchdog.stop()

or ``python run_servers.py --watchdog`` (see ``DOCUMENTATION.md``).
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import threading
import time
import traceback
from typing import Optional

DEFAULT_THRESHOLD = 1.0
DEFAULT_INTERVAL = 0.25
DEFAULT_REPEAT = 2.0


def add_argument(parser) -> None:
    """``--watchdog [SECONDS]`` for the server launchers."""
    parser.add_argument(
        "--watchdog", nargs="?", type=float, const=DEFAULT_THRESHOLD, default=None,
        metavar="SECONDS",
        help="log the blocking stack to <log_dir>/stalls.log whenever the event "
             f"loop stalls this long (default {DEFAULT_THRESHOLD:g}s)")


def start_for(threshold: Optional[float], logging_config) -> Optional["StallWatchdog"]:
    """Start a watchdog on the running loop when ``threshold`` is set.

    ``logging_config`` is the server's ``LogConfig``: stacks go to
    ``<log_dir>/stalls.log`` and one-line warnings to the ``watchdog`` log.
    """
    if threshold is None:
        return None
    from src.common.logger import setup_logger

    log_dir = logging_config.log_dir
    logger = setup_logger("watchdog", log_dir, logging_config.level, logging_config.console)
    watchdog = StallWatchdog(os.path.join(log_dir, "stalls.log"), threshold=threshold,
                             logger=logger)
    watchdog.start()
    return watchdog


class StallWatchdog:
    def __init__(self, log_path: Optional[str] = None,
                 threshold: float = DEFAULT_THRESHOLD,
                 interval: float = DEFAULT_INTERVAL,
                 repeat: float = DEFAULT_REPEAT,
                 logger: Optional[logging.Logger] = None) -> None:
        if threshold <= 0:
            raise ValueError("threshold must be positive")
        self.log_path = log_path
        self.threshold = float(threshold)
        self.interval = min(float(interval), self.threshold / 2)
        self.repeat = float(repeat)
        self.log = logger or logging.getLogger("stall_watchdog")
        self.stalls = 0             # stalls seen so far
        self.worst = 0.0            # longest stall in seconds
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._loop_thread_id = 0
        self._last_beat = time.monotonic()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------------
    def start(self, loop: Optional[asyncio.AbstractEventLoop] = None) -> None:
        """Start watching ``loop`` (default: the running loop).

        Must be called from the loop's own thread so the right stack is
        sampled.
        """
        self._loop = loop or asyncio.get_running_loop()
        self._loop_thread_id = threading.get_ident()
        self._last_beat = time.monotonic()
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="stall-watchdog",
                                        daemon=True)
        self._thread.start()
        self.log.info(f"Stall watchdog on: threshold {self.threshold:.2f}s"
                      + (f", stacks in {self.log_path}" if self.log_path else ""))

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    # ------------------------------------------------------------------
    def _beat(self) -> None:
        self._last_beat = time.monotonic()

    def _run(self) -> None:
        stall_started: Optional[float] = None
        last_dump = 0.0
        while not self._stop.wait(self.interval):
            loop = self._loop
            if loop is None or loop.is_closed():
                return
            try:
                loop.call_soon_threadsafe(self._beat)
            except RuntimeError:
                return                      # loop closed under us
            now = time.monotonic()
            stalled = now - self._last_beat
            if stalled >= self.threshold:
                if stall_started is None:
                    stall_started = self._last_beat
                    self.stalls += 1
                    last_dump = 0.0
                if now - last_dump >= self.repeat:
                    last_dump = now
                    self._write_stack(stalled)
            elif stall_started is not None:
                duration = self._last_beat - stall_started
                self.worst = max(self.worst, duration)
                self._write(f"=== {time.strftime('%H:%M:%S')} loop resumed after "
                            f"{duration:.2f}s ===\n")
                self.log.warning(f"Event loop stalled for {duration:.2f}s")
                stall_started = None

    def _write_stack(self, stalled: float) -> None:
        frame = sys._current_frames().get(self._loop_thread_id)
        stack = "".join(traceback.format_stack(frame)) if frame else "  <no frame>\n"
        self._write(f"\n=== {time.strftime('%H:%M:%S')} loop stalled "
                    f"{stalled:.1f}s ===\n{stack}")

    def _write(self, text: str) -> None:
        if not self.log_path:
            return
        try:
            folder = os.path.dirname(self.log_path)
            if folder:
                os.makedirs(folder, exist_ok=True)
            with open(self.log_path, "a", encoding="utf-8") as fh:
                fh.write(text)
        except OSError as exc:
            self.log.debug(f"Could not write {self.log_path}: {exc}")
