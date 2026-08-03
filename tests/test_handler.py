"""Handler-level tests: constructor validation, emit contract, close, independence.

Covers FR-1..6, FR-15/16/19/20 and NFR-3/4 from PRD.md.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable

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


# --- constructor / config (FR-2, FR-3) ---------------------------------------


def test_missing_credentials_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TG_TOKEN", raising=False)
    monkeypatch.delenv("TG_CHAT_ID", raising=False)
    with pytest.raises(TelegramConfigError):
        TelegramLoggingHandler(validate=False)


def test_invalid_token_format_raises() -> None:
    with pytest.raises(TelegramConfigError):
        TelegramLoggingHandler(token="garbage", chat_id=1, validate=False)


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


# --- emit contract (FR-4, NFR-3, NFR-4) --------------------------------------


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


# --- stats (FR-20) -----------------------------------------------------------


def test_stats_returns_immutable_snapshot(fast_handler_factory: HandlerFactory) -> None:
    handler = fast_handler_factory()
    stats = handler.stats
    assert isinstance(stats, HandlerStats)
    with pytest.raises(AttributeError):
        stats.sent = 5  # type: ignore[misc]  # frozen dataclass


# --- close (FR-16) -----------------------------------------------------------


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


# --- independence + concurrency (FR-5, FR-6) ---------------------------------


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
