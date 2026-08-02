"""Worker-thread resilience: an error on one record must not kill the thread.

Proves the ARCHITECTURE.md §4 formatter-failure and sender-failure rows by
driving ``WorkerThread`` directly with fakes (no real HTTP, no handler).
"""

from __future__ import annotations

import logging
import queue
from typing import Callable

import pytest

from tg_logging_handler.stats import StatsCollector
from tg_logging_handler.worker import SHUTDOWN, WorkerThread


class _FakeSender:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.sent: list[str] = []
        self.closed = False

    def send(self, text: str) -> None:
        if self.fail:
            raise RuntimeError("simulated send failure")
        self.sent.append(text)

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
    worker = WorkerThread(q, sender, stats, format_record, flush_interval=0.01)  # type: ignore[arg-type]
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
    assert "simulated send failure" in capsys.readouterr().err


def test_worker_closes_sender_on_exit() -> None:
    sender = _FakeSender()
    _run_worker(sender, lambda r: r.getMessage(), [])
    assert sender.closed is True
