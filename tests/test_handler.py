"""Handler-level tests: constructor validation, emit contract, close, independence."""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections.abc import Callable
from typing import Any, cast

import httpx
import pytest
import respx
from conftest import API_BASE, TEST_CHAT_ID, TEST_TOKEN

from tg_logging_handler import (
    HandlerStats,
    TelegramConfigError,
    TelegramLoggingHandler,
    TGLoggingHandler,
    __all__,
)
from tg_logging_handler.queue_policy import PutResult

HandlerFactory = Callable[..., TelegramLoggingHandler]
WaitFor = Callable[..., bool]


def _record(msg: str) -> logging.LogRecord:
    return logging.LogRecord("t", logging.ERROR, __file__, 1, msg, (), None)


def test_public_api_surface() -> None:
    assert TGLoggingHandler is TelegramLoggingHandler
    for name in (
        "TelegramLoggingHandler",
        "TGLoggingHandler",
        "HandlerStats",
        "TelegramConfigError",
    ):
        assert name in __all__


# --- constructor / config ---------------------------------------------------


def test_missing_credentials_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TG_TOKEN", raising=False)
    monkeypatch.delenv("TG_CHAT_ID", raising=False)
    with pytest.raises(TelegramConfigError):
        TelegramLoggingHandler(validate=False)


def test_invalid_token_format_raises() -> None:
    with pytest.raises(TelegramConfigError):
        TelegramLoggingHandler(token="garbage", chat_id=1, validate=False)


def test_failed_construction_unregisters_from_handler_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A half-built handler must not stay registered for exit-time shutdown()."""
    monkeypatch.delenv("TG_TOKEN", raising=False)
    monkeypatch.delenv("TG_CHAT_ID", raising=False)

    def alive() -> int:
        handler_list = cast(Any, logging)._handlerList
        return sum(1 for ref in handler_list if isinstance(ref(), TelegramLoggingHandler))

    before = alive()
    with pytest.raises(TelegramConfigError):
        TelegramLoggingHandler(validate=False)
    assert alive() == before


def test_env_fallback_constructs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TG_TOKEN", TEST_TOKEN)
    monkeypatch.setenv("TG_CHAT_ID", TEST_CHAT_ID)
    handler = TelegramLoggingHandler(validate=False, api_base_url=API_BASE)
    try:
        assert handler.level == logging.WARNING
    finally:
        handler.close()


def test_validate_true_success(mock_api: respx.MockRouter) -> None:
    handler = TelegramLoggingHandler(
        token=TEST_TOKEN, chat_id=TEST_CHAT_ID, validate=True, api_base_url=API_BASE
    )
    handler.close()
    getme = next(r for r in mock_api.routes if "getMe" in str(r.pattern))
    assert getme.call_count == 1


def test_validate_true_failure_raises(mock_api: respx.MockRouter) -> None:
    mock_api.get(f"{API_BASE}/bot{TEST_TOKEN}/getMe").mock(
        return_value=httpx.Response(401, json={"ok": False, "description": "Unauthorized"})
    )
    with pytest.raises(TelegramConfigError):
        TelegramLoggingHandler(
            token=TEST_TOKEN, chat_id=TEST_CHAT_ID, validate=True, api_base_url=API_BASE
        )


def test_validate_false_skips_getme(
    fast_handler_factory: HandlerFactory, mock_api: respx.MockRouter
) -> None:
    fast_handler_factory()
    getme = next(r for r in mock_api.routes if "getMe" in str(r.pattern))
    assert getme.call_count == 0


def test_level_kwarg_applied(fast_handler_factory: HandlerFactory) -> None:
    handler = fast_handler_factory(level="ERROR")
    assert handler.level == logging.ERROR


def test_topic_id_validation_raises() -> None:
    for bad in (0, -1, -100):
        with pytest.raises(ValueError):
            TelegramLoggingHandler(
                token=TEST_TOKEN, chat_id=TEST_CHAT_ID, validate=False, topic_id=bad
            )


def test_topic_id_none_is_allowed(fast_handler_factory: HandlerFactory) -> None:
    handler = fast_handler_factory(topic_id=None)  # None is allowed
    handler.close()


def test_topic_id_from_env_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TG_TOPIC_ID", "42")
    handler = TelegramLoggingHandler(
        token=TEST_TOKEN, chat_id=TEST_CHAT_ID, validate=False, api_base_url=API_BASE
    )
    assert handler._sender._message_thread_id == 42
    handler.close()


def test_topic_id_arg_wins_over_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TG_TOPIC_ID", "7")
    handler = TelegramLoggingHandler(
        token=TEST_TOKEN, chat_id=TEST_CHAT_ID, validate=False, topic_id=42, api_base_url=API_BASE
    )
    assert handler._sender._message_thread_id == 42
    handler.close()


def test_topic_id_invalid_env_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TG_TOPIC_ID", "not-a-number")
    with pytest.raises(ValueError):
        TelegramLoggingHandler(
            token=TEST_TOKEN, chat_id=TEST_CHAT_ID, validate=False, api_base_url=API_BASE
        )


def test_topic_id_reaches_the_wire(
    fast_handler_factory: HandlerFactory, mock_api: respx.MockRouter, wait_for: WaitFor
) -> None:
    import json

    handler = fast_handler_factory(topic_id=42)
    handler.emit(_record("topic log"))
    assert wait_for(lambda: handler.stats.sent == 1)
    route = next(r for r in mock_api.routes if "sendMessage" in str(r.pattern))
    body = json.loads(route.calls[0].request.content.decode())
    assert body["message_thread_id"] == 42


# --- emit contract ----------------------------------------------------------


def test_emit_never_blocks_on_slow_network(
    fast_handler_factory: HandlerFactory, mock_api: respx.MockRouter
) -> None:
    started = threading.Event()
    release = threading.Event()

    def slow_send(request: httpx.Request) -> httpx.Response:
        started.set()
        release.wait(5.0)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    mock_api.post(f"{API_BASE}/bot{TEST_TOKEN}/sendMessage").mock(side_effect=slow_send)
    handler = fast_handler_factory()
    try:
        start = time.perf_counter()
        handler.emit(_record("hello"))
        elapsed = time.perf_counter() - start
        assert started.wait(1.0), "worker never made the HTTP call"
        assert elapsed < 0.05  # emit only enqueues; the 5s send is on the worker
    finally:
        release.set()


def test_emit_never_raises_on_broken_record(fast_handler_factory: HandlerFactory) -> None:
    handler = fast_handler_factory()
    # msg='%s' with no args -> getMessage() raises inside emit; must be swallowed.
    broken = logging.LogRecord("t", logging.ERROR, __file__, 1, "%s %s", ("only-one",), None)
    handler.emit(broken)  # must not raise


# --- stats -------------------------------------------------------------------


def test_stats_returns_immutable_snapshot(fast_handler_factory: HandlerFactory) -> None:
    handler = fast_handler_factory()
    stats = handler.stats
    assert isinstance(stats, HandlerStats)
    with pytest.raises(AttributeError):
        stats.sent = 5  # type: ignore[misc]  # frozen dataclass


# --- close -------------------------------------------------------------------


def test_close_is_idempotent(fast_handler_factory: HandlerFactory) -> None:
    handler = fast_handler_factory()
    handler.close()
    handler.close()  # second call must be a no-op, not an error


def test_close_drains_queued_records(mock_api: respx.MockRouter) -> None:
    # Large flush_interval so records would NOT auto-flush; close() must drain them.
    handler = TelegramLoggingHandler(
        token=TEST_TOKEN,
        chat_id=TEST_CHAT_ID,
        validate=False,
        flush_interval=30.0,
        shutdown_timeout=3.0,
        api_base_url=API_BASE,
    )
    for i in range(3):
        handler.emit(_record(f"m{i}"))
    handler.close()
    assert handler.stats.sent == 3


# --- independence + concurrency ----------------------------------------------


def test_two_handlers_are_independent(
    fast_handler_factory: HandlerFactory, wait_for: WaitFor
) -> None:
    h1 = fast_handler_factory(chat_id="111")
    h2 = fast_handler_factory(chat_id="222")
    h1.emit(_record("only-h1"))
    assert wait_for(lambda: h1.stats.sent == 1)
    assert h2.stats.sent == 0  # h1's traffic never touched h2's counters


def test_concurrent_emit_loses_no_records(
    fast_handler_factory: HandlerFactory, wait_for: WaitFor
) -> None:
    handler = fast_handler_factory()

    def emit_batch(worker_id: int) -> None:
        for j in range(100):
            handler.emit(_record(f"w{worker_id}-{j}"))

    threads = [threading.Thread(target=emit_batch, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert wait_for(lambda: handler.stats.sent == 2000)
    stats = handler.stats
    assert stats.sent + stats.dropped + stats.failed == 2000


def test_close_stops_worker_even_when_queue_was_saturated(
    fast_handler_factory: HandlerFactory, mock_api: respx.MockRouter, wait_for: WaitFor
) -> None:
    # Regression: with a saturated queue the SHUTDOWN put is suppressed and
    # the worker must still terminate (event-backed shutdown); previously it
    # leaked the thread and the httpx client forever.
    handler = fast_handler_factory(queue_maxsize=1, flush_interval=5.0, shutdown_timeout=1.0)
    handler.emit(_record("first"))
    handler.emit(_record("second"))  # fills the queue: sentinel put is suppressed
    handler.close()
    assert wait_for(lambda: not handler._worker.is_alive())


def test_emit_after_close_drops_instead_of_blocking(
    mock_api: respx.MockRouter, wait_for: WaitFor
) -> None:
    # Regression: with queue_full_policy="block" a post-close emit on a full
    # queue hung the calling thread forever (no worker drains it). Now it is
    # dropped and counted.
    handler = TelegramLoggingHandler(
        token=TEST_TOKEN,
        chat_id=TEST_CHAT_ID,
        validate=False,
        queue_maxsize=1,
        queue_full_policy="block",
        flush_interval=30.0,
        shutdown_timeout=1.0,
        api_base_url=API_BASE,
    )
    handler.close()
    assert wait_for(lambda: not handler._worker.is_alive())
    before = handler.stats.dropped
    handler.emit(_record("post-close"))
    assert handler.stats.dropped == before + 1


def test_close_waits_for_in_flight_emit(mock_api: respx.MockRouter) -> None:
    # Regression: close() once interleaved its _closed write between an emit's
    # guard check and its enqueue, orphaning the record (counted queued but
    # never sent; block policy: hung forever). The close lock serializes emit
    # vs close: close() blocks until the in-flight emit has fully enqueued,
    # so nothing accepted before close() is lost.
    handler = TelegramLoggingHandler(
        token=TEST_TOKEN,
        chat_id=TEST_CHAT_ID,
        validate=False,
        queue_maxsize=2,
        queue_full_policy="block",
        flush_interval=5.0,
        shutdown_timeout=2.0,
        api_base_url=API_BASE,
    )
    started = threading.Event()
    release = threading.Event()
    original = handler._put_policy

    def slow_policy(q: queue.Queue[object], item: object) -> PutResult:
        result = original(q, item)
        started.set()  # emit is now inside the put (holding the close lock)
        release.wait(timeout=5.0)
        return result

    handler._put_policy = slow_policy

    def do_emit() -> None:
        handler.emit(_record("z"))

    emitter = threading.Thread(target=do_emit)
    emitter.start()
    assert started.wait(timeout=2.0)

    outcome: dict[str, float] = {}

    def do_close() -> None:
        t0 = time.monotonic()
        handler.close()
        outcome["elapsed"] = time.monotonic() - t0

    closer = threading.Thread(target=do_close)
    closer.start()
    time.sleep(0.2)
    assert "elapsed" not in outcome  # close() must wait for the in-flight emit
    release.set()
    emitter.join(timeout=2.0)
    closer.join(timeout=2.0)
    assert outcome["elapsed"] < 1.0  # close finished promptly once the emit did
    assert not handler._worker.is_alive()
    stats = handler.stats
    assert stats.queued == 1
    assert stats.sent == 1  # the in-flight record was drained, not orphaned


def test_emit_swallows_put_policy_exceptions(
    fast_handler_factory: HandlerFactory,
) -> None:
    # A policy that raises must never propagate out of emit (stdlib contract).
    handler = fast_handler_factory()

    def broken_policy(q: queue.Queue[object], item: object) -> PutResult:
        raise RuntimeError("policy exploded")

    handler._put_policy = broken_policy
    handler.emit(_record("x"))  # must not raise
    handler.close()


def test_emit_racing_close_drops_instead_of_orphaning(
    fast_handler_factory: HandlerFactory,
) -> None:
    # An emit whose snapshot was already taken when close() completed must hit
    # the authoritative closed check under the lock and drop+count instead of
    # enqueueing into a drained pipeline (counted queued, never sent).
    handler = fast_handler_factory()
    release = threading.Event()

    class SlowStr:
        def __str__(self) -> str:
            release.wait(timeout=2.0)
            return "boom"

    slow = SlowStr()
    # getMessage() formats msg % args, invoking SlowStr.__str__ -> the emit
    # pauses between the fast-path guard and the close-lock check.
    slow_record = logging.LogRecord("t", logging.ERROR, __file__, 1, "%s", (slow,), None)
    emitter = threading.Thread(target=handler.emit, args=(slow_record,))
    emitter.start()
    time.sleep(0.1)  # emit is now inside getMessage, before the lock
    handler._closed = True  # close() completed while the emit was in flight
    release.set()
    emitter.join(timeout=2.0)
    assert handler.stats.dropped == 1  # counted, not orphaned
    handler.close()  # idempotent no-op
