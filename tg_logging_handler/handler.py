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
import threading
from typing import Any, Literal, cast

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

        try:
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
            # escapes each record for parse_mode; the sender forwards parse_mode
            # to sendMessage so Telegram renders the (now-safe) entities.
            self._shutdown_timeout = shutdown_timeout
            self._closed = False
            # Guards the _closed flag against emit(), which checks it and then
            # enqueues: close() must not interleave its write between the
            # check and the put, or an in-flight emit would feed a queue no
            # one drains (block policy: hang forever; otherwise: a record
            # counted queued but never sent). The worker never takes this
            # lock, so close() cannot deadlock against the drain.
            self._close_lock = threading.Lock()
            self._stats = StatsCollector()
            self._queue: queue.Queue[object] = queue.Queue(maxsize=queue_maxsize)
            # Validate the policy name now so a typo fails fast at construction,
            # not silently at the first queue overflow.
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
        except Exception:
            # super().__init__ just registered a weakref to this half-built
            # instance in logging._handlerList; exit-time logging.shutdown()
            # would call close() on it and print a stray AttributeError after
            # the real construction error. Drop the weakref, then re-raise.
            handler_list = cast(Any, logging)._handlerList
            with cast(Any, logging)._lock:
                handler_list[:] = [ref for ref in handler_list if ref() is not self]
            raise

    def emit(self, record: logging.LogRecord) -> None:
        """Snapshot ``record`` and enqueue it. Never raises (stdlib contract)."""
        if self._closed:
            # Fast path; the authoritative check below is under the lock.
            self._stats.increment("dropped")
            return
        try:
            item = self._snapshot(record)
        except Exception:  # emit must never propagate
            self.handleError(record)
            return
        with self._close_lock:
            if self._closed:
                # close() interleaved between snapshot and put: no worker
                # drains this queue any more, so drop and count instead of
                # enqueueing into a dead pipeline (block policy would hang
                # this thread forever).
                self._stats.increment("dropped")
                return
            try:
                result = self._put_policy(self._queue, item)
            except Exception:  # emit must never propagate
                self.handleError(record)
                return
        # drop_oldest can lose an older record even while enqueueing the new
        # one, so count both outcomes from the PutResult.
        if result.enqueued:
            self._stats.increment("queued")
        self._stats.increment("dropped", result.dropped)

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
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
            # Signal stop twice: the event is authoritative (the sentinel below
            # is only a wakeup and can be lost to a saturated queue or
            # drop_oldest eviction); the worker drains everything queued first,
            # then exits. The join below runs outside the lock so an emit
            # blocked on a full queue (block policy) can finish draining.
            self._worker.shutdown()
            with contextlib.suppress(queue.Full):
                self._queue.put_nowait(SHUTDOWN)
        self._worker.join(timeout=self._shutdown_timeout)
        atexit.unregister(self.close)
        super().close()
