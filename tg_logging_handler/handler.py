"""The public ``TelegramLoggingHandler`` — a thin ``logging.Handler`` subclass.

Business logic (sending, batching, retries) lives in the worker/sender modules;
this class only wires them together and implements the stdlib handler contract
(CODING_STANDARDS.md §2).
"""

from __future__ import annotations

import atexit
import contextlib
import copy
import logging
import queue
from typing import Literal

from . import queue_policy
from .config import resolve_credentials, validate_token
from .sender import TelegramSender
from .stats import HandlerStats, StatsCollector
from .worker import SHUTDOWN, WorkerThread

__all__ = ["TelegramLoggingHandler"]


class TelegramLoggingHandler(logging.Handler):
    """A ``logging.Handler`` that delivers records to a Telegram chat.

    Records are enqueued by ``emit`` (never blocking on network I/O) and sent by
    a single background daemon thread. See ``docs/API_SPEC.md`` for the full
    parameter reference.

    Args:
        token: Bot token. Falls back to ``TG_TOKEN``. Raises if neither is set.
        chat_id: Target chat id. Falls back to ``TG_CHAT_ID``. Raises if neither is set.
        level: Minimum level, passed to ``setLevel``. Defaults to ``WARNING``.
        batch_size: Max records per outgoing message (``>= 1``). Batching lands in M1.
        flush_interval: Max seconds a partial batch waits (``>= 0``).
        max_retries: Retry budget for transient failures (``>= 0``). Honored from M1.
        overflow: Oversized-message policy. Honored from M2.
        parse_mode: Telegram parse mode forwarded to ``sendMessage``.
        queue_maxsize: Bounded queue size (``0`` = unbounded, memory risk).
        queue_full_policy: Behavior when the queue is full. Only ``drop_newest`` in M0.
        shutdown_timeout: Max seconds ``close`` waits for the worker to drain.
        validate: If ``True``, make a synchronous ``getMe`` check at construction.
        api_base_url: Bot API base URL (override for self-hosted servers/tests).

    Raises:
        TelegramConfigError: On missing credentials or (if ``validate``) a failed ``getMe``.
    """

    def __init__(
        self,
        token: str | None = None,
        chat_id: str | int | None = None,
        *,
        level: int | str = logging.WARNING,
        batch_size: int = 1,
        flush_interval: float = 5.0,
        max_retries: int = 3,
        overflow: Literal["split", "truncate", "drop"] = "split",
        parse_mode: Literal[None, "Markdown", "MarkdownV2", "HTML"] = None,
        queue_maxsize: int = 10_000,
        queue_full_policy: Literal["block", "drop_newest", "drop_oldest"] = "drop_newest",
        shutdown_timeout: float = 5.0,
        validate: bool = True,
        api_base_url: str = "https://api.telegram.org",
    ) -> None:
        super().__init__(level=level)

        if batch_size < 1:
            raise ValueError("batch_size must be >= 1")
        if flush_interval < 0:
            raise ValueError("flush_interval must be >= 0")
        if max_retries < 0:
            raise ValueError("max_retries must be >= 0")
        if queue_maxsize < 0:
            raise ValueError("queue_maxsize must be >= 0")

        resolved_token, resolved_chat = resolve_credentials(token, chat_id)
        if validate:
            validate_token(resolved_token, api_base_url)

        # ponytail: overflow→M2, queue_full_policy other-than-drop_newest→P3 are
        # still accepted-and-stored only. batch_size/flush_interval/max_retries are
        # live from M1 (batching + retry/backoff/429). Full signature keeps
        # dictConfig configs stable across milestones.
        self._shutdown_timeout = shutdown_timeout
        self._closed = False
        self._stats = StatsCollector()
        self._queue: queue.Queue[object] = queue.Queue(maxsize=queue_maxsize)
        self._sender = TelegramSender(
            resolved_token,
            resolved_chat,
            api_base_url,
            parse_mode=parse_mode,
            max_retries=max_retries,
        )
        self._worker = WorkerThread(
            record_queue=self._queue,
            sender=self._sender,
            stats=self._stats,
            format_record=self.format,
            batch_size=batch_size,
            flush_interval=flush_interval,
        )
        self._worker.start()
        atexit.register(self.close)

    def emit(self, record: logging.LogRecord) -> None:
        """Snapshot ``record`` and enqueue it. Never raises (stdlib contract)."""
        try:
            item = self._snapshot(record)
            if queue_policy.put_drop_newest(self._queue, item):
                self._stats.increment("queued")
            else:
                self._stats.increment("dropped")
        except Exception:  # emit must never propagate (ARCHITECTURE §3.2)
            self.handleError(record)

    @staticmethod
    def _snapshot(record: logging.LogRecord) -> logging.LogRecord:
        """Copy the record and merge args into the message.

        LogRecords can be mutated/recycled by some frameworks, so we copy before
        the worker touches it. Args are merged now (cheap) to drop the mutable
        list; ``exc_info`` is kept for the worker's formatter to render.
        """
        message = record.getMessage()
        snapshot = copy.copy(record)
        snapshot.msg = message
        snapshot.args = None
        return snapshot

    @property
    def stats(self) -> HandlerStats:
        """Return an immutable snapshot of the current counters."""
        return self._stats.snapshot()

    def close(self) -> None:
        """Drain the queue, stop the worker, release the client. Idempotent."""
        if self._closed:
            return
        self._closed = True
        # Queue saturated -> worker still stops via its own drain; worst case we
        # wait out shutdown_timeout below. ponytail: rare edge, not worth more.
        with contextlib.suppress(queue.Full):
            self._queue.put_nowait(SHUTDOWN)
        self._worker.join(timeout=self._shutdown_timeout)
        atexit.unregister(self.close)
        super().close()
