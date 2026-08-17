"""Thread-safe stats counters and the public immutable snapshot dataclass."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from threading import Lock

__all__ = ["HandlerStats", "StatsCollector"]


@dataclass(frozen=True)
class HandlerStats:
    """Immutable snapshot of cumulative counters since handler construction.

    Returned by ``TelegramLoggingHandler.stats``. All fields are cumulative
    counts; repeated reads reflect current totals.
    """

    queued: int = 0
    sent: int = 0
    batches_sent: int = 0
    retries: int = 0
    rate_limited: int = 0
    failed: int = 0
    dropped: int = 0


class StatsCollector:
    """Thread-safe counter bag owned by one handler instance.

    The worker thread mutates counters; ``handler.stats`` reads them from
    arbitrary application threads, so a lock guards all access; it is a cold
    path, a single lock is fine.
    """

    def __init__(self) -> None:
        self._lock = Lock()
        self._counts: dict[str, int] = asdict(HandlerStats())

    def increment(self, key: str, amount: int = 1) -> None:
        """Add ``amount`` to counter ``key`` (thread-safe)."""
        with self._lock:
            self._counts[key] += amount

    def snapshot(self) -> HandlerStats:
        """Return an immutable copy of the current counters."""
        with self._lock:
            return HandlerStats(**self._counts)
