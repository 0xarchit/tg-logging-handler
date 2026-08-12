"""Shared fixtures and helpers: mocked HTTP transport, fast handler factory."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterator
from typing import Any

import httpx
import pytest
import respx

from tg_logging_handler import TelegramLoggingHandler

# Only ever used against respx mocks; never a real Telegram call.
TEST_TOKEN = "123456:TEST-TOKEN"
TEST_CHAT_ID = "12345"
API_BASE = "https://api.telegram.org"

_logger_counter = 0


def _make_logger(name: str) -> logging.Logger:
    """Return a fresh, uniquely-named logger that never propagates to root."""
    global _logger_counter
    _logger_counter += 1
    logger = logging.getLogger(f"tg-test-{_logger_counter}.{name}")
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    return logger


@pytest.fixture
def make_logger() -> Callable[[str], logging.Logger]:
    """Fixture wrapper so tests can request ``make_logger`` as a dependency."""
    return _make_logger


def _ok(status_code: int, body: dict[str, Any]) -> httpx.Response:
    return httpx.Response(status_code, json=body)


@pytest.fixture
def mock_api(respx_mock: respx.MockRouter) -> respx.MockRouter:
    """Respx router wired for ``getMe`` and ``sendMessage`` success responses.

    Per-test overrides can re-register either route (respx: last match wins).
    """
    respx_mock.get(f"{API_BASE}/bot{TEST_TOKEN}/getMe").mock(
        return_value=_ok(200, {"ok": True, "result": {"id": 1, "is_bot": True}})
    )
    respx_mock.post(f"{API_BASE}/bot{TEST_TOKEN}/sendMessage").mock(
        return_value=_ok(200, {"ok": True, "result": {"message_id": 1}})
    )
    return respx_mock


@pytest.fixture
def fast_handler_factory(
    mock_api: respx.MockRouter,
) -> Iterator[Callable[..., TelegramLoggingHandler]]:
    """Factory for offline handlers (tiny flush_interval), all closed at teardown."""
    created: list[TelegramLoggingHandler] = []

    def _make(**kwargs: Any) -> TelegramLoggingHandler:
        kwargs.setdefault("token", TEST_TOKEN)
        kwargs.setdefault("chat_id", TEST_CHAT_ID)
        kwargs.setdefault("validate", False)
        kwargs.setdefault("flush_interval", 0.01)
        kwargs.setdefault("api_base_url", API_BASE)
        handler = TelegramLoggingHandler(**kwargs)
        created.append(handler)
        return handler

    yield _make

    for handler in created:
        handler.close()


@pytest.fixture
def wait_for() -> Callable[..., bool]:
    """Poll a predicate until true or timeout; avoids fixed sleeps for async work."""

    def _wait_for(predicate: Callable[[], bool], timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.01)
        return predicate()

    return _wait_for
