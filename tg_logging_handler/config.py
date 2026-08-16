"""Credential resolution and fail-fast startup validation."""

from __future__ import annotations

import os
import re

import httpx

from ._constants import DEFAULT_CONNECT_TIMEOUT, DEFAULT_READ_TIMEOUT
from .exceptions import TelegramConfigError

__all__ = ["resolve_credentials", "resolve_topic_id", "validate_token"]


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
            obviously invalid format.
    """
    token = token or os.environ.get("TG_TOKEN")
    if token:
        # Secrets loaded from files or env vars often carry a trailing
        # newline; strip it so validation and URL building see the real token.
        token = token.strip()
    if not token:
        raise TelegramConfigError(
            "No bot token provided. Pass token=... or set the TG_TOKEN environment variable."
        )
    if not _looks_like_bot_token(token):
        raise TelegramConfigError(_invalid_token_message(token))

    resolved_chat = chat_id if chat_id is not None else os.environ.get("TG_CHAT_ID")
    if resolved_chat is None or resolved_chat == "":
        raise TelegramConfigError(
            "No chat_id provided. Pass chat_id=... or set the TG_CHAT_ID environment variable."
        )

    return token, str(resolved_chat)


def resolve_topic_id(topic_id: int | None) -> int | None:
    """Resolve the forum topic id from args, falling back to ``TG_TOPIC_ID``.

    Args:
        topic_id: Explicit topic id, or ``None`` to read ``TG_TOPIC_ID``.

    Returns:
        A positive ``int`` topic id, or ``None`` when neither is set
        (messages then go to the group's General topic).

    Raises:
        ValueError: If the value is not an integer or not positive.
    """
    if topic_id is not None:
        if type(topic_id) is not int:
            # bool is an int subclass and floats compare fine, but both are
            # always mistakes here; anything else (e.g. str) would crash on
            # the range comparison below. Reject non-ints up front.
            raise ValueError(f"topic_id must be an integer, got {type(topic_id).__name__}")
        value = topic_id
    else:
        raw = os.environ.get("TG_TOPIC_ID")
        if raw is None or raw == "":
            return None
        try:
            value = int(raw)
        except ValueError as exc:
            raise ValueError(f"TG_TOPIC_ID must be an integer, got {raw!r}") from exc
    if value < 1:
        raise ValueError("topic_id must be a positive integer")
    return value


def _looks_like_bot_token(token: str) -> bool:
    """Heuristic: Telegram bot tokens are ``'<numeric bot_id>:<auth>'``."""
    return re.fullmatch(r"\d+:.+", token) is not None


def _invalid_token_message(token: str) -> str:
    """Build the invalid-token error, redacting the secret.

    Only ever shows the first/last 4 characters, and only when the token is
    long enough that those windows don't overlap; a short string could be the
    whole secret, so it is redacted entirely (never embed the raw token).
    """
    shown = f"{token[:4]!r}…{token[-4:]!r}" if len(token) >= 12 else "(redacted)"
    return f"Invalid bot token format: {shown}. Expected '<bot_id>:<auth_key>'."


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
