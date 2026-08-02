"""TelegramSender retry/backoff/429 logic (FR-10/11/12, ARCHITECTURE.md §4).

Pure-logic: HTTP is mocked with respx and sleep is injected, so no wall-clock
time passes. We assert on return values (retry counts), sleep calls (budget vs
Retry-After), and that send_with_retry never raises.
"""

from __future__ import annotations

import httpx
import respx

from tg_logging_handler.sender import TelegramSender

API_BASE = "https://api.telegram.org"
TOKEN = "123456:TEST-TOKEN"
CHAT = "12345"
SEND_URL = f"{API_BASE}/bot{TOKEN}/sendMessage"


class _RecordingSleep:
    """Fake sleep that records durations instead of blocking."""

    def __init__(self) -> None:
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


def _sender(sleep: _RecordingSleep, max_retries: int = 3) -> TelegramSender:
    return TelegramSender(TOKEN, CHAT, API_BASE, max_retries=max_retries, sleep=sleep)


def _ok() -> httpx.Response:
    return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})


@respx.mock
def test_first_try_success_no_retries() -> None:
    respx.post(SEND_URL).mock(return_value=_ok())
    sleep = _RecordingSleep()
    outcome = _sender(sleep).send_with_retry("hi")
    assert outcome.delivered is True
    assert outcome.retries == 0
    assert sleep.calls == []


@respx.mock
def test_transient_5xx_then_success_counts_retries() -> None:
    respx.post(SEND_URL).mock(side_effect=[httpx.Response(500), httpx.Response(500), _ok()])
    sleep = _RecordingSleep()
    outcome = _sender(sleep).send_with_retry("hi")
    assert outcome.delivered is True
    assert outcome.retries == 2
    assert len(sleep.calls) == 2  # two backoff sleeps, one per retry


@respx.mock
def test_retries_exhausted_returns_budget_and_never_raises() -> None:
    respx.post(SEND_URL).mock(return_value=httpx.Response(500))
    sleep = _RecordingSleep()
    # max_retries=3 -> 1 initial + 3 retries = 4 attempts, then give up.
    outcome = _sender(sleep, max_retries=3).send_with_retry("hi")
    assert outcome.delivered is False  # dropped after budget exhausted
    assert outcome.retries == 3
    assert len(sleep.calls) == 3


@respx.mock
def test_network_error_is_retryable() -> None:
    respx.post(SEND_URL).mock(side_effect=httpx.ConnectError("boom"))
    sleep = _RecordingSleep()
    outcome = _sender(sleep, max_retries=2).send_with_retry("hi")
    assert outcome.delivered is False
    assert outcome.retries == 2
    assert len(sleep.calls) == 2


@respx.mock
def test_permanent_4xx_is_not_retried() -> None:
    route = respx.post(SEND_URL).mock(return_value=httpx.Response(400, text="Bad Request"))
    sleep = _RecordingSleep()
    outcome = _sender(sleep).send_with_retry("hi")
    assert outcome.delivered is False  # permanent 4xx, not retried
    assert outcome.retries == 0  # no retries consumed
    assert route.call_count == 1
    assert sleep.calls == []


@respx.mock
def test_429_honors_retry_after_without_consuming_budget() -> None:
    respx.post(SEND_URL).mock(
        side_effect=[
            httpx.Response(429, headers={"retry-after": "7"}),
            httpx.Response(429, headers={"retry-after": "3"}),
            _ok(),
        ]
    )
    sleep = _RecordingSleep()
    # 429s do not consume the retry budget -> reported retries stay 0.
    outcome = _sender(sleep, max_retries=1).send_with_retry("hi")
    assert outcome.delivered is True
    assert outcome.retries == 0  # 429s do not consume the retry budget
    assert sleep.calls == [7.0, 3.0]  # slept exactly the Retry-After values


@respx.mock
def test_backoff_grows_and_is_bounded() -> None:
    respx.post(SEND_URL).mock(return_value=httpx.Response(503))
    sleep = _RecordingSleep()
    _sender(sleep, max_retries=4).send_with_retry("hi")
    # Jittered exponential: each sleep should be >= previous base trend within
    # ±25% jitter. Assert monotonic-ish growth via base bounds rather than exact.
    assert len(sleep.calls) == 4
    assert all(s <= 30.0 for s in sleep.calls)  # capped at _MAX_DELAY_SECONDS
    assert sleep.calls[0] <= sleep.calls[-1]  # later delays are larger


@respx.mock
def test_parse_mode_included_when_set() -> None:
    route = respx.post(SEND_URL).mock(return_value=_ok())
    sleep = _RecordingSleep()
    TelegramSender(TOKEN, CHAT, API_BASE, parse_mode="HTML", sleep=sleep).send_with_retry("hi")
    import json

    body = json.loads(route.calls[0].request.content.decode())
    assert body["parse_mode"] == "HTML"


@respx.mock
def test_429_waits_are_capped_so_a_stuck_429_cannot_spin_forever() -> None:
    # 11 consecutive 429s (cap is 10) must give up, not loop forever.
    from tg_logging_handler.sender import _MAX_RATE_LIMIT_WAITS

    respx.post(SEND_URL).mock(return_value=httpx.Response(429, headers={"retry-after": "1"}))
    sleep = _RecordingSleep()
    outcome = _sender(sleep).send_with_retry("hi")
    assert outcome.delivered is False  # gave up after the cap
    assert outcome.retries == 0  # 429s never consume the retry budget
    assert len(sleep.calls) == _MAX_RATE_LIMIT_WAITS  # slept exactly cap times
    assert sleep.calls == [1.0] * _MAX_RATE_LIMIT_WAITS


@respx.mock
def test_429_with_non_numeric_retry_after_uses_default() -> None:
    # RFC 9110 allows an HTTP-date (e.g. "Wed, 21 Oct 2015 07:28:00 GMT");
    # float() can't parse it, so we must fall back instead of crashing.
    respx.post(SEND_URL).mock(
        side_effect=[
            httpx.Response(429, headers={"retry-after": "Wed, 21 Oct 2015 07:28:00 GMT"}),
            _ok(),
        ]
    )
    sleep = _RecordingSleep()
    outcome = _sender(sleep).send_with_retry("hi")
    assert outcome.delivered is True
    assert outcome.retries == 0
    assert sleep.calls == [1.0]  # conservative default
