"""Unit tests for BatchAccumulator: size/interval triggers, shutdown, quiet-idle.

Covers FR-7/8/9 and the "flush timer inactive while empty" rule. No real
sleeps — a scripted monotonic clock is injected (TESTING.md §1/§3). The clock
is scripted (not wall-clock) so the interval math is deterministic AND the
queue never blocks: ``remaining`` hits ``<= 0`` exactly as the queue drains, so
``collect`` breaks instead of waiting on an empty queue.
"""

from __future__ import annotations

import logging
import queue

import pytest

from tg_logging_handler.batching import BatchAccumulator

Shutdown = object()


def _record(msg: str) -> logging.LogRecord:
    return logging.LogRecord("t", logging.ERROR, __file__, 1, msg, (), None)


class ScriptedClock:
    """now() yields the next scripted tick, holding the last value when drained."""

    def __init__(self, *ticks: float) -> None:
        self._ticks = list(ticks)
        self._last = 0.0

    def now(self) -> float:
        if self._ticks:
            self._last = self._ticks.pop(0)
        return self._last


def _mk(
    batch_size: int = 5, flush_interval: float = 1.0, clock: ScriptedClock | None = None
) -> tuple[BatchAccumulator, queue.Queue[object]]:
    acc = BatchAccumulator(batch_size, flush_interval, now=(clock or ScriptedClock()).now)
    return acc, queue.Queue()


def _put(q: queue.Queue[object], *msgs: str) -> None:
    for m in msgs:
        q.put(_record(m))


def test_size_trigger_flushes_full_batch() -> None:
    acc, q = _mk(batch_size=3, flush_interval=10.0)
    _put(q, "a", "b", "c")
    batch, shutdown = acc.collect(q, Shutdown, timeout=0)
    assert len(batch) == 3
    assert not shutdown


def test_interval_trigger_flushes_partial_batch() -> None:
    # ticks: start=0, iter1 remaining=5>0 (collect b), iter2 remaining=0 (break).
    acc, q = _mk(batch_size=10, flush_interval=5.0, clock=ScriptedClock(0.0, 0.0, 5.0))
    _put(q, "a", "b")
    batch, _ = acc.collect(q, Shutdown, timeout=0)
    assert len(batch) == 2


def test_interval_not_elapsed_keeps_collecting() -> None:
    # Sub-interval arrivals join one batch; break only when the clock reaches 5.
    acc, q = _mk(batch_size=10, flush_interval=5.0, clock=ScriptedClock(0.0, 1.0, 2.0, 5.0))
    _put(q, "a", "b", "c")
    batch, _ = acc.collect(q, Shutdown, timeout=0)
    assert len(batch) == 3


def test_empty_collect_times_out_without_records() -> None:
    acc, q = _mk()
    batch, shutdown = acc.collect(q, Shutdown, timeout=0.0)
    assert batch == []
    assert not shutdown


def test_shutdown_sentinel_flushes_partial_batch() -> None:
    acc, q = _mk(batch_size=10, flush_interval=10.0)
    _put(q, "a", "b")
    q.put(Shutdown)
    batch, shutdown = acc.collect(q, Shutdown, timeout=0)
    assert len(batch) == 2
    assert shutdown


def test_shutdown_when_batch_empty() -> None:
    acc, q = _mk()
    q.put(Shutdown)
    batch, shutdown = acc.collect(q, Shutdown, timeout=0)
    assert batch == []
    assert shutdown


def test_quiet_handler_idles_without_waking() -> None:
    """An empty batch never starts the flush timer — collect just waits on get."""
    acc, q = _mk(flush_interval=30.0)
    batch, shutdown = acc.collect(q, Shutdown, timeout=0.02)
    assert batch == []
    assert not shutdown


def test_constructor_validates_args() -> None:
    with pytest.raises(ValueError):
        BatchAccumulator(0, 1.0)
    with pytest.raises(ValueError):
        BatchAccumulator(1, -1.0)
