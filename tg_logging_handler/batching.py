"""BatchAccumulator: size/interval-triggered batching.

The worker thread owns one accumulator per handler. Records are drained from
the queue into the current batch; the batch is flushed (returned to the caller)
when either ``batch_size`` records accumulate or ``flush_interval`` seconds
elapse since the batch became non-empty, whichever comes first.

The flush timer is *inactive while the batch is empty*: a quiet handler must
not wake its worker every ``flush_interval``. Only the arrival of the first
record starts the clock, which keeps idle CPU at zero and makes
``flush_interval=0`` mean "flush as soon as anything arrives".
"""

from __future__ import annotations

import queue
import time
from collections.abc import Callable
from logging import LogRecord

__all__ = ["BatchAccumulator"]

# Longest single wait inside a partial batch. Waiting beyond this (a long
# ``flush_interval``) would keep the worker blind to shutdown for the whole
# interval; the loop re-checks ``stop`` after every capped wait, so shutdown
# latency stays bounded to roughly this value even when the SHUTDOWN sentinel
# is lost to a saturated queue or drop_oldest eviction.
_MAX_RECORD_WAIT = 1.0


class BatchAccumulator:
    """Collect queued records into batches flushed by size or elapsed time.

    Not thread-safe by design; owned exclusively by the worker thread.
    ``now`` is injectable for deterministic tests.

    ``collect`` returns ``(batch, shutdown)``: ``shutdown`` is ``True`` when a
    SHUTDOWN sentinel was seen, in which case any partial batch is flushed
    first and the worker should stop after sending it.
    """

    def __init__(
        self,
        batch_size: int,
        flush_interval: float,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        if batch_size < 1:
            raise ValueError("batch_size must be >= 1")
        if flush_interval < 0:
            raise ValueError("flush_interval must be >= 0")
        self._batch_size = batch_size
        self._flush_interval = flush_interval
        self._now = now

    def collect(
        self,
        record_queue: queue.Queue[object],
        shutdown_sentinel: object,
        timeout: float | None = None,
        stop: Callable[[], bool] | None = None,
    ) -> tuple[list[LogRecord], bool]:
        """Drain up to one batch from ``record_queue``.

        Blocks up to ``timeout`` seconds waiting for the first record when the
        batch is empty. Returns ``([], False)`` if nothing arrived and no
        partial batch was pending.

        ``stop``, when given, is polled after a capped wait that elapsed with
        nothing arriving; a ``True`` return flushes the partial batch
        immediately so the caller can honour a shutdown request without
        waiting out a long ``flush_interval``. Records that keep flowing are
        never split by ``stop``: once shutdown is requested the caller still
        wants everything already accepted drained, and a batch still growing
        would otherwise flush per record.

        ``batch`` and ``batch_started`` are locals, not instance state: each
        call drains whatever has arrived and returns it, so a batch never
        spans two calls. (The flush timer starts on the first record and is
        inactive while the batch is empty.)
        """
        shutdown = False
        batch: list[LogRecord] = []
        batch_started: float | None = None
        try:
            item = record_queue.get(timeout=timeout)
        except queue.Empty:
            return [], False
        if item is shutdown_sentinel:
            shutdown = True
            record_queue.task_done()  # the sentinel was consumed like any item
        else:
            assert isinstance(item, LogRecord)  # only LogRecords are enqueued
            batch.append(item)
            batch_started = self._now()

        while batch and not shutdown and len(batch) < self._batch_size:
            assert batch_started is not None  # non-empty batch implies started
            remaining = self._flush_interval - (self._now() - batch_started)
            if remaining <= 0:
                break  # interval elapsed → flush partial batch
            try:
                item = record_queue.get(timeout=min(remaining, _MAX_RECORD_WAIT))
            except queue.Empty:
                if min(remaining, _MAX_RECORD_WAIT) < remaining:
                    if stop is not None and stop():
                        # A long capped wait elapsed with nothing arriving and a
                        # shutdown is pending: flush now. Never checked between
                        # arriving records — a flowing batch must keep growing.
                        break
                    continue  # capped wait elapsed; re-check the interval
                break  # nothing more arrived within the interval → flush partial
            if item is shutdown_sentinel:
                shutdown = True
                # consumed item; batch records are task_done'd by the caller
                record_queue.task_done()
                break
            assert isinstance(item, LogRecord)
            batch.append(item)

        return batch, shutdown
