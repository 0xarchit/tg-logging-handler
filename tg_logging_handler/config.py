"""Credential resolution and fail-fast startup validation."""

from __future__ import annotations

import os
import re

import httpx

from ._constants import DEFAULT_CONNECT_TIMEOUT, DEFAULT_READ_TIMEOUT
from .exceptions import TelegramConfigError

__all__ = ["resolve_credentials", "validate_token"]


def resolve_credentials(
    token: str | None,
    chat_id: str | int | None,
) -> tuple[str, str]:
    """Resolve the bot token and chat id from args, falling back to env vars.

    Args:
        token: Explicit token, or ``None`` to read ``TG_TOKEN``.
        chat_id: Explicit chat id, or ``None`` to read ``TG_CHAT_ID``.

    Returns:
        A ``(token, chat_id)`` tuple, both as ``str``.

    Raises:
        TelegramConfigError: If either value is missing or the token has an
            obviously invalid format (FR-3).
    """
    token = token or os.environ.get("TG_TOKEN")
    if not token:
        raise TelegramConfigError(
            "No bot token provided. Pass token=... or set the TG_TOKEN environment variable."
        )
    if not _looks_like_bot_token(token):
        raise TelegramConfigError(
            f"Invalid bot token format: {token[:4]!r}…{token[-4:]!r}. "
            f"Expected '<bot_id>:<auth_key>'."
        )

    resolved_chat = chat_id if chat_id is not None else os.environ.get("TG_CHAT_ID")
    if resolved_chat is None or resolved_chat == "":
        raise TelegramConfigError(
            "No chat_id provided. Pass chat_id=... or set the TG_CHAT_ID environment variable."
        )

    return token, str(resolved_chat)


def _looks_like_bot_token(token: str) -> bool:
    """Heuristic: Telegram bot tokens are ``'<numeric bot_id>:<auth>'``."""
    return re.fullmatch(r"\d+:.+", token) is not None


def validate_token(token: str, api_base_url: str) -> None:
    """Verify the token with a single synchronous ``getMe`` call (fail fast).

    Args:
        token: The resolved bot token.
        api_base_url: Base URL of the Bot API (overridable for self-hosted/tests).

    Raises:
        TelegramConfigError: If the request fails or Telegram reports the token invalid.
    """
    url = f"{api_base_url}/bot{token}/getMe"
    timeout = httpx.Timeout(DEFAULT_READ_TIMEOUT, connect=DEFAULT_CONNECT_TIMEOUT)
    try:
        response = httpx.get(url, timeout=timeout)
    except httpx.HTTPError as exc:
        raise TelegramConfigError(
            f"Could not reach Telegram to validate the token: {exc}. "
            "Pass validate=False to skip this check in offline environments."
        ) from exc

    if response.status_code != 200 or not response.json().get("ok", False):
        raise TelegramConfigError(
            f"Telegram rejected the token (getMe returned HTTP {response.status_code}). "
            "Check that the bot token is correct."
        )
