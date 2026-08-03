"""The worker thread: consumes the queue, batches records, drives the sender.

M1: the loop drains queued records into a :class:`BatchAccumulator` and sends
each flushed batch as one or more ``sendMessage`` calls, with retry/backoff/429
handling inside :meth:`TelegramSender.send_with_retry`. The loop is
exception-safe end to end — an unexpected error reports to stderr, increments
``failed``, and the loop continues; it must never kill the thread
(CODING_STANDARDS.md §4, ARCHITECTURE.md §3.4/§4).
"""

from __future__ import annotations

import queue
import threading
from collections.abc import Callable
from logging import LogRecord

from . import _diagnostics
from ._constants import TELEGRAM_MAX_MESSAGE_LENGTH
from .batching import BatchAccumulator
from .formatting import escape
from .overflow import prepare_messages
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
        batch_size: int,
        flush_interval: float,
        overflow: str = "split",
        parse_mode: str | None = None,
    ) -> None:
        super().__init__(name="tg-logging-handler-worker", daemon=True)
        self._queue = record_queue
        self._sender = sender
        self._stats = stats
        self._format_record = format_record
        # batch_size==1 (default) makes the accumulator flush every record.
        self._accumulator = BatchAccumulator(batch_size=batch_size, flush_interval=flush_interval)
        self._overflow = overflow
        self._parse_mode = parse_mode

    def run(self) -> None:
        try:
            self._loop()
        finally:
            self._sender.close()

    def _loop(self) -> None:
        while True:
            # Empty batch -> block up to 1s waiting for a record (a quiet
            # handler must not busy-poll); partial batch -> collect until size
            # or interval, whichever comes first (FR-9). Shutdown flushes any
            # partial batch first (FR-16 drain), then stops.
            batch, shutdown = self._accumulator.collect(self._queue, SHUTDOWN, timeout=1.0)
            if batch:
                try:
                    self._process_batch(batch)
                except Exception as exc:  # broad by design, see comment below
                    # A single bad record (format error) must never take the
                    # worker thread down (ARCHITECTURE.md §4 formatter-failure).
                    self._stats.increment("failed")
                    _diagnostics.report(f"failed to send batch: {exc}")
                finally:
                    for _ in batch:
                        self._queue.task_done()
            elif shutdown:
                return
            else:
                # timed out with an empty batch — loop and wait again
                continue

    def _process_batch(self, batch: list[LogRecord]) -> None:
        """Format the batch, apply the overflow policy, and send each message.

        Formatting happens once per record, up front, so a formatter that
        raises fails the whole batch before any send attempt (the _loop catch
        increments ``failed`` and the worker keeps running). An oversized batch
        becomes one or more messages per the ``overflow`` policy (FR-13): split
        into numbered parts, truncated to one message, or dropped entirely. The
        batch's records count as ``sent`` only if every part is delivered;
        otherwise they count as ``failed`` (conservative: a partially delivered
        split still flags the batch, ARCHITECTURE.md §4).
        """
        # Escape each record's text for the parse_mode before joining, so no
        # stray char in a message/traceback breaks Telegram's parser (FR-18).
        # Escaping per-record (not the joined blob) keeps the newline separators
        # literal — they are the separators, not content to escape.
        lines = [escape(self._format_record(r), self._parse_mode) for r in batch]
        text = "\n".join(lines)
        messages = prepare_messages(
            text, self._overflow, TELEGRAM_MAX_MESSAGE_LENGTH, parse_mode=self._parse_mode
        )
        if not messages:
            # overflow="drop" on oversized text: nothing goes on the wire (FR-13).
            self._stats.increment("dropped", len(batch))
            _diagnostics.report(
                f"dropping oversized batch of {len(batch)} record(s) (overflow='drop')"
            )
            return

        delivered_all = True
        for message in messages:
            outcome = self._sender.send_with_retry(message)
            self._stats.increment("retries", outcome.retries)
            delivered_all = delivered_all and outcome.delivered

        if delivered_all:
            self._stats.increment("sent", len(batch))
            self._stats.increment("batches_sent")
        else:
            # Retries exhausted or permanent 4xx on at least one part: the batch
            # is treated as dropped, so it counts as failed, not sent
            # (ARCHITECTURE.md §4, FR-12). The sender already reported the cause.
            self._stats.increment("failed", len(batch))
            _diagnostics.report(f"dropping batch of {len(batch)} record(s) after send failure")
