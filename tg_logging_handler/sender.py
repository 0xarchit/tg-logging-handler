"""HTTP sender: one ``httpx.Client`` per handler, lazily created on the worker thread.

Adds retry with exponential backoff + jitter, 429 ``Retry-After``
handling that does not consume the retry budget, and permanent-4xx
fail-fast. The client is created lazily inside
:meth:`TelegramSender.send_with_retry` so it is bound to the worker thread that uses it,
never the constructing thread. ``sleep`` is injectable
so the backoff math is testable with a fake clock.
"""

from __future__ import annotations

import math
import random
import time
from collections.abc import Callable
from dataclasses import dataclass

import httpx

from . import _diagnostics
from ._constants import DEFAULT_CONNECT_TIMEOUT, DEFAULT_READ_TIMEOUT
from .exceptions import TelegramSendError

__all__ = ["SendOutcome", "TelegramSendError", "TelegramSender"]

_BASE_DELAY_SECONDS = 0.5  # first retry waits ~0.5s, then ~1s, ~2s ... (capped)
_MAX_DELAY_SECONDS = 30.0
_JITTER_FRACTION = 0.25  # ±25% jitter around each backoff delay
_MAX_RATE_LIMIT_WAITS = 10  # cap consecutive 429 waits so a stuck 429 can't spin forever


@dataclass(frozen=True)
class SendOutcome:
    """Result of one ``send_with_retry`` call: delivered or not, and retry count.

    ``delivered=False`` means the message was dropped: after retries were
    exhausted, after a permanent failure, or after too many consecutive 429
    waits even when the retry budget remains; the worker counts it as
    ``failed`` so ``sent`` stays accurate.
    """

    delivered: bool
    retries: int


class TelegramSender:
    """Sends messages to Telegram's ``sendMessage`` endpoint with retry/backoff.

    A single ``httpx.Client`` is created lazily on first send and bound to the
    worker thread (never the constructing thread). Retries happen here, inside
    the worker, so the application thread is never blocked by network I/O.
    """

    def __init__(
        self,
        token: str,
        chat_id: str,
        api_base_url: str,
        parse_mode: str | None = None,
        max_retries: int = 3,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._url = f"{api_base_url}/bot{token}/sendMessage"
        self._chat_id = chat_id
        self._parse_mode = parse_mode
        self._max_retries = max_retries
        self._sleep = sleep
        self._client: httpx.Client | None = None
        # One-time heads-up when rate limiting first starts, so a 429 storm does
        # not silently retry forever; re-sending it on every 429 would only add
        # to the flood (see _notify_rate_limited).
        self._notified_rate_limit = False

    def _get_client(self) -> httpx.Client:
        """Return the worker-thread-local client, creating it on first use."""
        if self._client is None:
            timeout = httpx.Timeout(DEFAULT_READ_TIMEOUT, connect=DEFAULT_CONNECT_TIMEOUT)
            self._client = httpx.Client(timeout=timeout)
        return self._client

    def send_with_retry(self, text: str) -> SendOutcome:
        """POST one message, retrying transient failures.

        Returns a :class:`SendOutcome`: ``delivered`` is ``True`` only if
        Telegram accepted the message, and ``retries`` counts the transient
        retries performed (0 on first-try success). Never raises; exhausted or
        permanent failures return ``delivered=False`` so the worker counts them
        as ``failed`` and reports to stderr.
        429s honor ``Retry-After`` without consuming the retry budget,
        capped at ``_MAX_RATE_LIMIT_WAITS`` consecutive waits so a stuck 429
        cannot spin the worker forever; permanent 4xx are not retried at all,
        since retrying a broken request just wastes the budget.
        """
        attempts = 0
        rate_limited = 0
        delay = _BASE_DELAY_SECONDS
        while True:
            try:
                self._send_once(text)
                return SendOutcome(delivered=True, retries=attempts)
            except TelegramSendError as exc:
                if exc.retry_after is not None:
                    # 429 rate limit: honor Retry-After without consuming the
                    # retry budget, but cap consecutive waits so a
                    # stuck/malicious 429 can't spin the worker forever and
                    # block drain/shutdown.
                    if not self._notified_rate_limit:
                        # First 429 of this sender's life: fire a one-time notice
                        # into the chat before the first backoff, so an operator
                        # sees that rate limiting started even if the flood keeps
                        # up and every later send is dropped.
                        self._notified_rate_limit = True
                        self._notify_rate_limited()
                    if rate_limited >= _MAX_RATE_LIMIT_WAITS:
                        return SendOutcome(delivered=False, retries=attempts)
                    rate_limited += 1
                    # Clamp the server's Retry-After to [0, _MAX_DELAY_SECONDS]
                    # so one huge (or negative) header value can't stall the
                    # worker for hours or crash time.sleep.
                    self._sleep(min(max(exc.retry_after, 0.0), _MAX_DELAY_SECONDS))
                    continue
                if not exc.retryable or attempts >= self._max_retries:
                    return SendOutcome(delivered=False, retries=attempts)
                attempts += 1
                rate_limited = 0  # a non-429 retry breaks the consecutive-429 run
                jittered = delay * (1.0 + _JITTER_FRACTION * (2.0 * random.random() - 1.0))
                self._sleep(max(jittered, 0.0))
                delay = min(delay * 2.0, _MAX_DELAY_SECONDS)

    def _notify_rate_limited(self) -> None:
        """Best-effort, one-time heads-up that Telegram started rate limiting.

        Posted directly (no retry, no 429 classification) so it can never
        recurse into this notice path or block the worker on a backoff. Any
        failure (likely, since we are being rate limited) is reported to stderr
        and swallowed. The message body carries no token or chat context.
        """
        text = (
            "tg-logging-handler: a 429 (Too Many Requests) just came back from "
            "the Telegram API. Logs will keep retrying and may keep hitting the "
            "rate limit; recommend checking it manually."
        )
        payload = {"chat_id": self._chat_id, "text": text, "disable_web_page_preview": True}
        try:
            self._get_client().post(self._url, json=payload)
        except httpx.HTTPError as exc:
            _diagnostics.report(f"could not send 429 heads-up notification: {exc}")

    def _send_once(self, text: str) -> None:
        """POST one message; raise ``TelegramSendError`` on any failure.

        Classifies the failure as retryable (network error, 5xx), a 429 (with
        ``Retry-After``), or permanent (other 4xx). ``TelegramSendError`` is
        internal-only and never propagates past ``send_with_retry``.
        """
        payload: dict[str, object] = {
            "chat_id": self._chat_id,
            "text": text,
            "disable_web_page_preview": True,
        }
        if self._parse_mode is not None:
            payload["parse_mode"] = self._parse_mode

        try:
            response = self._get_client().post(self._url, json=payload)
        except httpx.HTTPError as exc:
            raise TelegramSendError(str(exc), retryable=True) from exc

        if response.status_code == 429:
            try:
                retry_after = float(response.headers.get("retry-after", "1.0"))
            except ValueError:
                # Retry-After may be an HTTP-date (RFC 9110), not seconds. We
                # don't parse dates; fall back to a conservative 1s default.
                retry_after = 1.0
            if math.isnan(retry_after) or retry_after == float("inf"):
                retry_after = 1.0
            raise TelegramSendError(
                f"rate limited (429) for {retry_after}s", retryable=True, retry_after=retry_after
            )
        if 400 <= response.status_code < 500:
            raise TelegramSendError(
                f"permanent error {response.status_code}: {response.text}",
                retryable=False,
            )
        if response.status_code >= 500:
            raise TelegramSendError(
                f"server error {response.status_code}: {response.text}", retryable=True
            )

    def close(self) -> None:
        """Close the underlying client. Safe to call when never opened."""
        if self._client is not None:
            self._client.close()
            self._client = None
