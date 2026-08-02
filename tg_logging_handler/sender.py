"""HTTP sender — one ``httpx.Client`` per handler, lazily created on the worker thread.

M0 scope: send-only, no retry/backoff/429 handling (those arrive in M1). The
client is created lazily inside :meth:`TelegramSender.send` so it is bound to
the worker thread that uses it, never the constructing thread
(ARCHITECTURE.md §3.6).
"""

from __future__ import annotations

import httpx

from ._constants import DEFAULT_CONNECT_TIMEOUT, DEFAULT_READ_TIMEOUT

__all__ = ["TelegramSender"]


class TelegramSender:
    """Sends a single message to Telegram's ``sendMessage`` endpoint."""

    def __init__(
        self,
        token: str,
        chat_id: str,
        api_base_url: str,
        parse_mode: str | None = None,
    ) -> None:
        self._url = f"{api_base_url}/bot{token}/sendMessage"
        self._chat_id = chat_id
        self._parse_mode = parse_mode
        self._client: httpx.Client | None = None

    def _get_client(self) -> httpx.Client:
        """Return the worker-thread-local client, creating it on first use."""
        if self._client is None:
            timeout = httpx.Timeout(DEFAULT_READ_TIMEOUT, connect=DEFAULT_CONNECT_TIMEOUT)
            self._client = httpx.Client(timeout=timeout)
        return self._client

    def send(self, text: str) -> None:
        """POST one message. Raises ``httpx.HTTPError`` on network/HTTP failure.

        The caller (worker loop) is responsible for catching failures — this
        method deliberately lets errors propagate so the worker can count and
        report them.
        """
        payload: dict[str, object] = {
            "chat_id": self._chat_id,
            "text": text,
            "disable_web_page_preview": True,
        }
        if self._parse_mode is not None:
            payload["parse_mode"] = self._parse_mode

        response = self._get_client().post(self._url, json=payload)
        response.raise_for_status()

    def close(self) -> None:
        """Close the underlying client. Safe to call when never opened."""
        if self._client is not None:
            self._client.close()
            self._client = None
