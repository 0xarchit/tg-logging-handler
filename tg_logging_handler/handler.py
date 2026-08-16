"""The public ``TelegramLoggingHandler``: a thin ``logging.Handler`` subclass.

Business logic (sending, batching, retries) lives in the worker/sender modules;
this class only wires them together and implements the stdlib handler contract.
"""

from __future__ import annotations

import atexit
import contextlib
import copy
import logging
import queue
from typing import Literal

from . import queue_policy
from .config import resolve_credentials, resolve_topic_id, validate_token
from .sender import TelegramSender
from .stats import HandlerStats, StatsCollector
from .worker import SHUTDOWN, WorkerThread

__all__ = ["TelegramLoggingHandler"]


class TelegramLoggingHandler(logging.Handler):
    """A ``logging.Handler`` that delivers records to a Telegram chat.

    Records are enqueued by ``emit`` (never blocking on network I/O) and sent by
    a single background daemon thread.

    Args:
        token: Bot token. Falls back to ``TG_TOKEN``. Raises if neither is set.
        chat_id: Target chat id. Falls back to ``TG_CHAT_ID``. Raises if neither is set.
        topic_id: Target forum topic id. Falls back to ``TG_TOPIC_ID``. When set
            and the chat is a forum supergroup, messages go to that topic; when
            unset they go to the group's General topic.
        level: Minimum level, passed to ``setLevel``. Defaults to ``WARNING``.
        batch_size: Max records per outgoing message (``>= 1``).
        flush_interval: Max seconds a partial batch waits (``>= 0``).
        max_retries: Retry budget for transient failures (``>= 0``).
        overflow: Oversized-message policy.
        parse_mode: Telegram parse mode forwarded to ``sendMessage``.
        queue_maxsize: Bounded queue size (``0`` = unbounded, memory risk).
        queue_full_policy: Behavior when the queue is full.
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
        topic_id: int | None = None,
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
        resolved_topic = resolve_topic_id(topic_id)
        if validate:
            validate_token(resolved_token, api_base_url)

        # Full signature is live: batching + retry/backoff/429, overflow +
        # queue policies + full stats, parse_mode escaping. The worker
        # escapes each record for parse_mode; the sender forwards parse_mode to
        # sendMessage so Telegram renders the (now-safe) entities.
        self._shutdown_timeout = shutdown_timeout
        self._closed = False
        self._stats = StatsCollector()
        self._queue: queue.Queue[object] = queue.Queue(maxsize=queue_maxsize)
        # Validate the policy name now so a typo fails fast at construction, not
        # silently at the first queue overflow.
        self._put_policy = queue_policy.select_put_policy(queue_full_policy)
        self._sender = TelegramSender(
            resolved_token,
            resolved_chat,
            api_base_url,
            parse_mode=parse_mode,
            max_retries=max_retries,
            message_thread_id=resolved_topic,
        )
        self._worker = WorkerThread(
            record_queue=self._queue,
            sender=self._sender,
            stats=self._stats,
            format_record=self.format,
            batch_size=batch_size,
            flush_interval=flush_interval,
            overflow=overflow,
            parse_mode=parse_mode,
        )
        self._worker.start()
        atexit.register(self.close)

    def emit(self, record: logging.LogRecord) -> None:
        """Snapshot ``record`` and enqueue it. Never raises (stdlib contract)."""
        try:
            item = self._snapshot(record)
            result = self._put_policy(self._queue, item)
            # drop_oldest can lose an older record even while enqueueing the new
            # one, so count both outcomes from the PutResult.
            if result.enqueued:
                self._stats.increment("queued")
            self._stats.increment("dropped", result.dropped)
        except Exception:  # emit must never propagate
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
