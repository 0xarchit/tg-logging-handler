"""End-to-end tests: a real logger with the handler attached → Telegram API calls.

Covers TESTING.md §2.3 scenarios 1-2 against a mocked Bot API.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable

import respx
from conftest import TEST_CHAT_ID, TEST_TOKEN

from tg_logging_handler import TelegramLoggingHandler

HandlerFactory = Callable[..., TelegramLoggingHandler]
LoggerFactory = Callable[..., logging.Logger]
WaitFor = Callable[..., bool]


def _body(route: respx.Route) -> dict[str, object]:
    content = route.calls[0].request.content.decode()
    result: dict[str, object] = json.loads(content)
    return result


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
