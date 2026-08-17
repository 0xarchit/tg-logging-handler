"""Worker-thread resilience: an error on one record must not kill the thread.

Proves the formatter-failure and sender-failure resilience rows by
driving ``WorkerThread`` directly with fakes (no real HTTP, no handler).
"""

from __future__ import annotations

import logging
import queue
from collections.abc import Callable
from typing import cast

import pytest

from tg_logging_handler.sender import SendOutcome, TelegramSender
from tg_logging_handler.stats import StatsCollector
from tg_logging_handler.worker import SHUTDOWN, WorkerThread


class _FakeSender:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.sent: list[str] = []
        self.closed = False

    def send_with_retry(self, text: str) -> SendOutcome:
        if self.fail:
            return SendOutcome(delivered=False, retries=0)
        self.sent.append(text)
        return SendOutcome(delivered=True, retries=0)

    def close(self) -> None:
        self.closed = True


def _record(msg: str) -> logging.LogRecord:
    return logging.LogRecord("t", logging.ERROR, __file__, 1, msg, (), None)


def _run_worker(
    sender: _FakeSender,
    format_record: Callable[[logging.LogRecord], str],
    items: list[object],
) -> StatsCollector:
    q: queue.Queue[object] = queue.Queue()
    stats = StatsCollector()
    worker = WorkerThread(q, sender, stats, format_record, batch_size=1, flush_interval=0.01)  # type: ignore[arg-type]
    worker.start()
    for item in items:
        q.put(item)
    q.put(SHUTDOWN)
    worker.join(timeout=3.0)
    assert not worker.is_alive()
    return stats


def test_worker_survives_formatter_error(capsys: pytest.CaptureFixture[str]) -> None:
    calls = {"n": 0}

    def flaky_format(record: logging.LogRecord) -> str:
        calls["n"] += 1
        if calls["n"] == 1:
            raise ValueError("formatter exploded")
        return record.getMessage()

    stats = _run_worker(_FakeSender(), flaky_format, [_record("bad"), _record("good")])

    snapshot = stats.snapshot()
    assert snapshot.failed == 1
    assert snapshot.sent == 1  # loop kept going after the failure
    assert "formatter exploded" in capsys.readouterr().err


def test_worker_survives_sender_error(capsys: pytest.CaptureFixture[str]) -> None:
    sender = _FakeSender(fail=True)
    stats = _run_worker(sender, lambda r: r.getMessage(), [_record("x"), _record("y")])

    snapshot = stats.snapshot()
    assert snapshot.failed == 2
    assert snapshot.sent == 0
    assert "after send failure" in capsys.readouterr().err


def test_worker_closes_sender_on_exit() -> None:
    sender = _FakeSender()
    _run_worker(sender, lambda r: r.getMessage(), [])
    assert sender.closed is True


def test_worker_stops_after_flushing_partial_batch_on_shutdown() -> None:
    # A shutdown sentinel arriving mid-drain (partial batch + sentinel, batch
    # larger than 1) must make the worker exit right after flushing it, not
    # loop forever on the now-empty queue.
    q: queue.Queue[object] = queue.Queue()
    sender = _FakeSender()
    stats = StatsCollector()
    worker = WorkerThread(
        q,
        cast(TelegramSender, sender),
        stats,
        lambda r: r.getMessage(),
        batch_size=2,
        flush_interval=0.01,
    )
    worker.start()
    q.put(_record("one"))
    q.put(SHUTDOWN)
    worker.join(timeout=2.0)
    assert not worker.is_alive()
    assert sender.sent == ["one"]
    assert stats.snapshot().sent == 1


def test_worker_stops_when_sentinel_is_lost_to_full_queue() -> None:
    # Regression: with a saturated queue the handler's sentinel put is
    # suppressed, and drop_oldest can even evict it. The shutdown event alone
    # must still stop the worker after everything queued is drained.
    q: queue.Queue[object] = queue.Queue(maxsize=1)
    q.put_nowait(_record("only"))
    sender = _FakeSender()
    stats = StatsCollector()
    worker = WorkerThread(
        q,
        cast(TelegramSender, sender),
        stats,
        lambda r: r.getMessage(),
        batch_size=1,
        flush_interval=0.01,
    )
    worker.start()
    worker.shutdown()  # no sentinel involved: the event must be enough
    worker.join(timeout=3.0)
    assert not worker.is_alive()
    assert sender.sent == ["only"]  # drained before exit


def test_shutdown_event_interrupts_long_interval_wait() -> None:
    # Regression: with a partial batch pending inside a long flush_interval,
    # shutdown used to wait the whole interval out (the mid-batch get only
    # saw the sentinel, which a saturated queue can lose). The event is now
    # polled mid-batch, so the worker exits promptly and still flushes the
    # pending record.
    q: queue.Queue[object] = queue.Queue()
    q.put_nowait(_record("pending"))
    sender = _FakeSender()
    stats = StatsCollector()
    worker = WorkerThread(
        q,
        cast(TelegramSender, sender),
        stats,
        lambda r: r.getMessage(),
        batch_size=10,  # one record never fills the batch
        flush_interval=60.0,  # the long wait shutdown must cut short
    )
    worker.start()
    worker.shutdown()  # event only; no sentinel on the queue at all
    worker.join(timeout=3.0)
    assert not worker.is_alive()
    assert sender.sent == ["pending"]  # partial batch flushed before exit


def test_worker_counts_rate_limited_waits_in_stats() -> None:
    class _RateLimitedSender(_FakeSender):
        def send_with_retry(self, text: str) -> SendOutcome:
            self.sent.append(text)
            # 429s never consume the retry budget, but every wait must count.
            return SendOutcome(delivered=True, retries=0, rate_limited=3)

    stats = _run_worker(_RateLimitedSender(), lambda r: r.getMessage(), [_record("x")])
    assert stats.snapshot().rate_limited == 3


def test_sentinel_consumption_has_matching_task_done() -> None:
    # Regression: the sentinel is consumed with a get() but never got a
    # task_done(), so unfinished_tasks stayed > 0 forever and any
    # queue.join() would hang.
    q: queue.Queue[object] = queue.Queue()
    stats = StatsCollector()
    worker = WorkerThread(
        q,
        cast(TelegramSender, _FakeSender()),
        stats,
        lambda r: r.getMessage(),
        batch_size=1,
        flush_interval=0.01,
    )
    worker.start()
    q.put(_record("x"))
    q.put(SHUTDOWN)
    worker.join(timeout=3.0)
    assert not worker.is_alive()
    assert q.unfinished_tasks == 0
