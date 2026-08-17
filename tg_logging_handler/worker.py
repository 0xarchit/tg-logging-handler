"""The worker thread: consumes the queue, batches records, drives the sender.

The loop drains queued records into a :class:`BatchAccumulator` and sends
batches that reach the sender as one or more ``sendMessage`` calls, with
retry/backoff/429 handling inside :meth:`TelegramSender.send_with_retry`.
Only exceptions raised while processing a batch via
:meth:`WorkerThread._process_batch` are caught: the error reports to stderr,
increments ``failed``, and the loop continues; it must never kill the thread.
Batches that never reach the sender (formatting error, overflow="drop") are
accounted for without any ``sendMessage`` call.
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
        # Authoritative stop signal: close() sets it. The SHUTDOWN sentinel is
        # only a wakeup hint and can be lost (suppressed put on a saturated
        # queue, drop_oldest eviction), so the loop must not rely on it alone.
        self._shutdown_event = threading.Event()
        # batch_size==1 (default) makes the accumulator flush every record.
        self._accumulator = BatchAccumulator(batch_size=batch_size, flush_interval=flush_interval)
        self._overflow = overflow
        self._parse_mode = parse_mode

    def shutdown(self) -> None:
        """Request a stop after everything currently queued has been drained."""
        self._shutdown_event.set()

    def run(self) -> None:
        try:
            self._loop()
        finally:
            self._sender.close()

    def _loop(self) -> None:
        """Run the drain/send cycle until shutdown.

        Failed-count accounting is deliberate:
        - an exception while processing a batch increments ``failed`` by
          ``len(batch)`` (every record in that batch never reached the wire);
        - a partial or failed send — ``SendOutcome(delivered=False)`` from
          retries exhausted, a permanent failure, or too many consecutive 429
          waits (``_MAX_RATE_LIMIT_WAITS``) even when the retry budget
          remains — increments ``failed`` by ``len(batch)``;
        - overflow="drop" increments ``dropped`` by ``len(batch)``.

        Split batches count as ``sent`` only when every part is delivered;
        otherwise they count as ``failed`` (conservative: a partially
        delivered split still flags the batch).
        """
        while True:
            if self._shutdown_event.is_set() and self._queue.empty():
                # The sentinel can be lost (suppressed put on a saturated
                # queue, drop_oldest eviction); the event is the reliable stop
                # signal. Everything queued before shutdown is drained first.
                return
            # Empty batch -> block up to 1s waiting for a record (a quiet
            # handler must not busy-poll); partial batch -> collect until size
            # or interval, whichever comes first. The accumulator polls the
            # shutdown event between its capped waits, so a shutdown is
            # honoured within ~1s even when a long flush_interval has a
            # partial batch pending and the sentinel was lost. Every batch is
            # flushed first; nothing already accepted is dropped.
            batch, shutdown = self._accumulator.collect(
                self._queue, SHUTDOWN, timeout=1.0, stop=self._shutdown_event.is_set
            )
            if batch:
                try:
                    self._process_batch(batch)
                except Exception as exc:  # broad by design, see comment below
                    # A single bad record (format error) must never take the
                    # worker thread down.
                    self._stats.increment("failed", len(batch))
                    _diagnostics.report(f"failed to send batch: {exc}")
                finally:
                    for _ in batch:
                        self._queue.task_done()
                if shutdown:
                    # Shutdown arrived while draining this batch; stop after
                    # flushing it instead of looping forever on an empty queue.
                    return
            elif shutdown:
                return
            else:
                # timed out with an empty batch; loop and wait again
                continue

    def _process_batch(self, batch: list[LogRecord]) -> None:
        """Format the batch, apply the overflow policy, and send each message.

        Formatting happens once per record, up front, so a formatter that
        raises fails the whole batch before any send attempt (the _loop catch
        increments ``failed`` and the worker keeps running). An oversized batch
        becomes one or more messages per the ``overflow`` policy: split
        into numbered parts, truncated to one message, or dropped entirely.
        With ``overflow="split"`` the batch's records count as ``sent`` only
        if every part is delivered; otherwise they count as ``failed``
        (conservative: a partially delivered split still flags the batch).
        With ``overflow="drop"`` the records never reach the sender and
        increment ``dropped`` instead.
        """
        # Escape each record's text for the parse_mode before joining, so no
        # stray char in a message/traceback breaks Telegram's parser.
        # Escaping per-record (not the joined blob) keeps the newline separators
        # literal; they are the separators, not content to escape.
        lines = [escape(self._format_record(r), self._parse_mode) for r in batch]
        text = "\n".join(lines)
        messages = prepare_messages(
            text, self._overflow, TELEGRAM_MAX_MESSAGE_LENGTH, parse_mode=self._parse_mode
        )
        if not messages:
            # overflow="drop" on oversized text: nothing goes on the wire.
            self._stats.increment("dropped", len(batch))
            _diagnostics.report(
                f"dropping oversized batch of {len(batch)} record(s) (overflow='drop')"
            )
            return

        delivered_all = True
        for message in messages:
            outcome = self._sender.send_with_retry(message)
            self._stats.increment("retries", outcome.retries)
            # 429 Retry-After waits count separately: they never consume the
            # retry budget, but a rate-limit storm must still be visible.
            self._stats.increment("rate_limited", outcome.rate_limited)
            delivered_all = delivered_all and outcome.delivered

        if delivered_all:
            self._stats.increment("sent", len(batch))
            self._stats.increment("batches_sent")
        else:
            # send_with_retry returned delivered=False on at least one part
            # (retries exhausted, permanent failure, or the consecutive-429
            # wait cap): the batch is treated as dropped, so it counts as
            # failed, not sent. The worker's report below is the only record
            # of the drop.
            self._stats.increment("failed", len(batch))
            _diagnostics.report(f"dropping batch of {len(batch)} record(s) after send failure")
