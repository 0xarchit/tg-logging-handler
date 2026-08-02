"""End-to-end tests: a real logger with the handler attached → Telegram API calls.

Covers TESTING.md §2.3 scenarios 1-2 against a mocked Bot API.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable

import httpx
import respx
from conftest import API_BASE, TEST_CHAT_ID, TEST_TOKEN

from tg_logging_handler import TelegramLoggingHandler

HandlerFactory = Callable[..., TelegramLoggingHandler]
LoggerFactory = Callable[..., logging.Logger]
WaitFor = Callable[..., bool]


def _body(route: respx.Route) -> dict[str, object]:
    content = route.calls[0].request.content.decode()
    result: dict[str, object] = json.loads(content)
    return result


def _batch_texts(route: respx.Route) -> list[str]:
    """Flatten the per-batch text bodies of every sendMessage call."""
    return [json.loads(c.request.content.decode())["text"] for c in route.calls]


def test_logger_error_sends_one_message(
    make_logger: LoggerFactory,
    mock_api: respx.MockRouter,
    fast_handler_factory: HandlerFactory,
    wait_for: WaitFor,
) -> None:
    logger = make_logger("scenario1")
    handler = fast_handler_factory()
    logger.addHandler(handler)

    logger.error("hello")
    route = next(r for r in mock_api.routes if "sendMessage" in str(r.pattern))
    assert wait_for(lambda: route.call_count == 1)
    logger.handlers.clear()

    request = route.calls[0].request
    assert request.method == "POST"
    assert request.url.path == f"/bot{TEST_TOKEN}/sendMessage"
    body = _body(route)
    assert body["chat_id"] == TEST_CHAT_ID
    assert body["text"] == "hello"
    assert body["disable_web_page_preview"] is True
    assert "parse_mode" not in body


def test_level_filter_blocks_below_threshold(
    make_logger: LoggerFactory,
    mock_api: respx.MockRouter,
    fast_handler_factory: HandlerFactory,
    wait_for: WaitFor,
) -> None:
    logger = make_logger("scenario2")
    handler = fast_handler_factory(level="WARNING")
    logger.addHandler(handler)

    logger.debug("noise")
    logger.info("also noise")
    logger.warning("real problem")

    route = next(r for r in mock_api.routes if "sendMessage" in str(r.pattern))
    assert wait_for(lambda: route.call_count == 1)
    logger.handlers.clear()

    assert _body(route)["text"] == "real problem"


def test_burst_of_100_is_batched(
    make_logger: LoggerFactory,
    mock_api: respx.MockRouter,
    fast_handler_factory: HandlerFactory,
    wait_for: WaitFor,
) -> None:
    """M1 exit: 100 logs at batch_size=10 → ~10 batched sends, not 100 (FR-7)."""
    logger = make_logger("burst")
    # Large interval so only the size trigger fires: each batch is exactly full.
    handler = fast_handler_factory(batch_size=10, flush_interval=30.0)
    logger.addHandler(handler)

    for i in range(100):
        logger.error("msg-%d", i)
    route = next(r for r in mock_api.routes if "sendMessage" in str(r.pattern))
    assert wait_for(lambda: route.call_count == 10)
    logger.handlers.clear()

    # Every record delivered, none lost, and each wire message carries 10 lines.
    assert handler.stats.sent == 100
    assert handler.stats.batches_sent == 10
    assert all(text.count("\n") == 9 for text in _batch_texts(route))


def test_outage_degrades_gracefully_without_raising(
    make_logger: LoggerFactory,
    mock_api: respx.MockRouter,
    fast_handler_factory: HandlerFactory,
    wait_for: WaitFor,
) -> None:
    """M1 exit: a total Bot API outage drops batches and counts them, never raising."""
    mock_api.post(f"{API_BASE}/bot{TEST_TOKEN}/sendMessage").mock(return_value=httpx.Response(500))
    logger = make_logger("outage")
    handler = fast_handler_factory(batch_size=5, max_retries=1)
    logger.addHandler(handler)

    for i in range(5):
        logger.error("boom-%d", i)  # emit must not raise even while the API is down
    assert wait_for(lambda: handler.stats.failed == 5)
    logger.handlers.clear()

    stats = handler.stats
    assert stats.sent == 0
    assert stats.retries >= 1  # the one retry was attempted before giving up


def test_recovery_after_transient_outage(
    make_logger: LoggerFactory,
    mock_api: respx.MockRouter,
    fast_handler_factory: HandlerFactory,
    wait_for: WaitFor,
) -> None:
    """A 500 that recovers to 200 mid-run: the retry succeeds, nothing is dropped."""
    responses = [httpx.Response(500), httpx.Response(200, json={"ok": True, "result": {}})]
    mock_api.post(f"{API_BASE}/bot{TEST_TOKEN}/sendMessage").mock(side_effect=responses)
    logger = make_logger("recovery")
    handler = fast_handler_factory(batch_size=1, max_retries=3)
    logger.addHandler(handler)

    logger.error("flaky")
    assert wait_for(lambda: handler.stats.sent == 1)
    logger.handlers.clear()

    stats = handler.stats
    assert stats.failed == 0
    assert stats.retries == 1  # one 500 → one retry → delivered
