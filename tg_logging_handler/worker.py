"""The worker thread: consumes the queue, formats records, drives the sender.

M0 shape: one record in, one ``sendMessage`` out (no batching yet). The loop is
exception-safe end to end — an unexpected error reports to stderr, increments
``failed``, and the loop continues; it must never kill the thread
(CODING_STANDARDS.md §4, ARCHITECTURE.md §3.4/§4).
"""

from __future__ import annotations

import queue
import threading
from logging import LogRecord
from typing import Callable

from . import _diagnostics
from .sender import TelegramSender
from .stats import StatsCollector

__all__ = ["SHUTDOWN", "WorkerThread"]

# Sentinel pushed onto the queue by close() to unblock the worker immediately
# instead of waiting out flush_interval. Any items queued before it are drained
# first (FIFO), so a clean shutdown loses nothing already accepted.
SHUTDOWN = object()


class WorkerThread(threading.Thread):
    """Single daemon consumer thread owned by one handler instance."""

    def __init__(
        self,
        record_queue: queue.Queue[object],
        sender: TelegramSender,
        stats: StatsCollector,
        format_record: Callable[[LogRecord], str],
        flush_interval: float,
    ) -> None:
        super().__init__(name="tg-logging-handler-worker", daemon=True)
        self._queue = record_queue
        self._sender = sender
        self._stats = stats
        self._format_record = format_record
        # queue.get needs a positive timeout; treat flush_interval==0 as a short poll.
        self._get_timeout = flush_interval if flush_interval > 0 else 0.1

    def run(self) -> None:
        try:
            self._loop()
        finally:
            self._sender.close()

    def _loop(self) -> None:
        while True:
            try:
                item = self._queue.get(timeout=self._get_timeout)
            except queue.Empty:
                continue

            if item is SHUTDOWN:
                self._queue.task_done()
                return

            # Broad by design: a single bad record (format error, transient
            # network failure) must never take the worker thread down.
            try:
                self._process(item)
            except Exception as exc:  # broad by design, see comment above
                self._stats.increment("failed")
                _diagnostics.report(f"failed to send log record: {exc}")
            finally:
                self._queue.task_done()

    def _process(self, item: object) -> None:
        assert isinstance(item, LogRecord)  # only LogRecords are enqueued
        text = self._format_record(item)
        self._sender.send(text)
        self._stats.increment("sent")
        self._stats.increment("batches_sent")
