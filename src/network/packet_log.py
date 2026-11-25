"""Packet logging for debugging.

Packet dumps used to be written *and flushed* on the asyncio thread for every
packet.  A single ``flush()`` on a slow Windows disk blocked the event loop for
up to several seconds (the stall watchdog caught 20 such stalls in 10 minutes).
The logger now only enqueues records from the game loop; a background writer
formats and writes them, flushing once per second.  ``Close`` (also registered
with :mod:`atexit`) drains the queue and flushes so a normal shutdown or a
caught crash still leaves a useful log.  Packet logging can be disabled with
``logging.packet_log: false`` in ``config.yaml``.
"""
from __future__ import annotations

import atexit
import os
import queue
import threading
import time
from datetime import datetime
from enum import IntEnum
from typing import Optional

from src.protocol.opcodes import Opcodes


class Direction(IntEnum):
    CLIENT_TO_SERVER = 0
    SERVER_TO_CLIENT = 1


#: Seconds between background flushes.  A hard crash loses at most this much
#: of the tail of the packet log.
FLUSH_INTERVAL = 1.0

#: Bound on queued records.  A disk that cannot keep up with the packet rate
#: must never grow memory without limit; the dropped count is summarised when
#: the log is closed.
MAX_PENDING_RECORDS = 50_000


class PacketLog:
    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        if hasattr(self, "_initialized"):
            return
        self._initialized = True
        self._file = None
        self.path = None
        self._log_lock = threading.Lock()
        self._initialize_flag = threading.Event()
        self._session_started = False
        self._closed = False
        self._stop = threading.Event()
        self._queue: "queue.Queue[Optional[tuple]]" = queue.Queue(
            maxsize=MAX_PENDING_RECORDS)
        self._thread: Optional[threading.Thread] = None
        self._dropped = 0
        atexit.register(self.Close)

    def Initialize(self, log_dir: str = "./logs", enabled: bool = True,
                   flush_interval: float = FLUSH_INTERVAL) -> None:
        """Open the session's packet log once.

        ``run_servers.py`` constructs the auth server and the world server in
        one process and both call this, so a second call must be a no-op.  It
        used to open a second file, which left the first one empty (the file
        the *other* server had been writing to) and made a crashed client
        session impossible to diagnose.  The timestamp carries microseconds so
        two servers started inside the same second cannot collide on a name and
        truncate each other.

        ``enabled=False`` leaves the logger inactive: no file is created, no
        writer thread is started, and callers' ``CanLogPacket()`` checks are
        false.
        """
        with self._log_lock:
            if self._session_started or self._file is not None:
                return
            self._session_started = True
            self._closed = False
            self._stop.clear()
            self._dropped = 0
            if not enabled:
                self._file = None
                self.path = None
                self._initialize_flag.set()
                return
            os.makedirs(log_dir, exist_ok=True)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            filepath = os.path.join(log_dir, f"packets_{timestamp}.log")
            try:
                self._file = open(filepath, "w", encoding="utf-8")
                self.path = filepath
            except Exception as exc:
                print(f"Failed to initialize packet log: {exc}")
                self._file = None
                self.path = None
                self._initialize_flag.set()
                return
            self._initialize_flag.set()
            self._thread = threading.Thread(
                target=self._writer_loop,
                args=(max(0.05, float(flush_interval)),),
                name="packet-log-writer",
                daemon=True,
            )
            self._thread.start()

    def CanLogPacket(self) -> bool:
        return self._file is not None

    def LogPacket(self, packet_data: bytes, opcode: int, direction: Direction,
                  addr: str = "127.0.0.1", port: int = 0) -> None:
        """Queue one packet record; never performs disk I/O on the caller."""
        if self._file is None:
            return
        try:
            self._queue.put_nowait((time.time(), direction, addr, port,
                                    int(opcode), packet_data))
        except queue.Full:
            # Dropping is preferable to stalling the game loop; the close
            # summary records how many records were lost.
            self._dropped += 1

    def _writer_loop(self, flush_interval: float) -> None:
        last_flush = time.monotonic()
        while True:
            remaining = flush_interval - (time.monotonic() - last_flush)
            try:
                record = self._queue.get(timeout=max(0.0, remaining))
            except queue.Empty:
                if self._stop.is_set():
                    break
                self._flush()
                last_flush = time.monotonic()
                continue
            if record is None:
                break
            self._write_record(record)
            if time.monotonic() - last_flush >= flush_interval:
                self._flush()
                last_flush = time.monotonic()
        self._flush()

    def _write_record(self, record: tuple) -> None:
        when, direction, addr, port, opcode, packet_data = record
        try:
            opcode_name = self._get_opcode_name(opcode)
            dir_str = "C->S" if direction == Direction.CLIENT_TO_SERVER else "S->C"
            timestamp = datetime.fromtimestamp(when).strftime("%H:%M:%S.%f")[:-3]
            self._write_text(
                f"[{timestamp}] {dir_str} {addr}:{port} {opcode_name}"
                f" (0x{opcode:04X}) [{len(packet_data)} bytes]\n")
            if packet_data:
                hex_str = " ".join(f"{byte:02X}" for byte in packet_data)
                self._write_text(f"  {hex_str}\n")
            self._write_text("\n")
        except Exception:
            pass

    def _write_text(self, text: str) -> None:
        if self._file is not None:
            try:
                self._file.write(text)
            except Exception:
                pass

    def _flush(self) -> None:
        if self._file is not None:
            try:
                self._file.flush()
            except Exception:
                pass

    def Close(self) -> None:
        """Drain queued records and flush/close the file (idempotent)."""
        with self._log_lock:
            if self._closed:
                return
            self._closed = True
            thread = self._thread
            self._thread = None
        self._stop.set()
        if thread is not None:
            try:
                self._queue.put_nowait(None)
            except queue.Full:
                pass
            thread.join(timeout=5.0)
        # A packet racing with shutdown may have been queued after the writer's
        # sentinel; the writer has stopped, so drain the remainder here.
        while True:
            try:
                record = self._queue.get_nowait()
            except queue.Empty:
                break
            if record is not None:
                self._write_record(record)
        with self._log_lock:
            if self._dropped:
                self._write_text(
                    f"  [{self._dropped} packet record(s) dropped]\n\n")
            self._flush()
            if self._file is not None:
                try:
                    self._file.close()
                except Exception:
                    pass
                self._file = None
            self._session_started = False
            self._initialize_flag.clear()

    def _get_opcode_name(self, opcode: int) -> str:
        try:
            return Opcodes(opcode).name
        except ValueError:
            return "UNKNOWN"


def get_packet_log() -> PacketLog:
    return PacketLog()


# Convenience alias
sPacketLog = get_packet_log()
