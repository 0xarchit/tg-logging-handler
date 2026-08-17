"""Smoke test: a real package-usage example, credentials read from the env.

Two layers:

* Offline smoke (always runs, incl. CI): the public surface (every export, the
  convenience alias, all constructor flags/enums, the fail-fast validation
  errors, and the stats snapshot) works without touching the network.
* Live end-to-end (network): reads ``TG_TOKEN`` / ``TG_CHAT_ID`` and actually
  delivers logs to Telegram, exercising ``validate=True``, batching, the
  ``split`` overflow policy, ``parse_mode`` escaping, tracebacks, and the stats
  counters. Auto-skipped when those env vars are unset, so ``pytest`` stays
  green offline; export both to run the real thing.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable
from typing import Any

import pytest

from tg_logging_handler import (
    HandlerStats,
    TelegramConfigError,
    TelegramLoggingHandler,
    TGLoggingHandler,
    __all__,
    __version__,
)

# A well-formed token that never leaves the process: offline tests either fail
# before any send (bad format) or never emit, so it only ever builds the URL.
OFFLINE_TOKEN = "123456:OFFLINE-SMOKE-TOKEN"
OFFLINE_CHAT = "12345"

# Live tests need real, working credentials; skip cleanly when they are absent.
requires_live = pytest.mark.skipif(
    not (os.environ.get("TG_TOKEN") and os.environ.get("TG_CHAT_ID")),
    reason="set TG_TOKEN and TG_CHAT_ID to run the live end-to-end smoke test",
)


# --------------------------------------------------------------------------- #
# Offline smoke: public surface, flags, and validation; no network, no env.
# --------------------------------------------------------------------------- #


def test_public_exports_and_version_are_importable() -> None:
    # Everything __all__ promises is importable, and the alias is the same class.
    assert set(__all__) == {
        "HandlerStats",
        "TGLoggingHandler",
        "TelegramConfigError",
        "TelegramLoggingHandler",
        "__version__",
    }
    assert TGLoggingHandler is TelegramLoggingHandler
    assert issubclass(TelegramLoggingHandler, logging.Handler)
    assert issubclass(TelegramConfigError, Exception)
    assert isinstance(__version__, str)


def test_missing_token_raises_config_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TG_TOKEN", raising=False)
    monkeypatch.delenv("TG_CHAT_ID", raising=False)
    with pytest.raises(TelegramConfigError, match="No bot token"):
        TelegramLoggingHandler(chat_id=OFFLINE_CHAT, validate=False)


def test_missing_chat_id_raises_config_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TG_CHAT_ID", raising=False)
    with pytest.raises(TelegramConfigError, match="No chat_id"):
        TelegramLoggingHandler(token=OFFLINE_TOKEN, validate=False)


def test_invalid_token_format_is_rejected_without_leaking_the_secret() -> None:
    # A malformed token fails fast, and the error never echoes the raw secret.
    secret = "totally-not-a-valid-bot-token-secret"
    with pytest.raises(TelegramConfigError) as excinfo:
        TelegramLoggingHandler(token=secret, chat_id=OFFLINE_CHAT, validate=False)
    assert secret not in str(excinfo.value)


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"batch_size": 0}, "batch_size"),
        ({"flush_interval": -1.0}, "flush_interval"),
        ({"max_retries": -1}, "max_retries"),
        ({"queue_maxsize": -1}, "queue_maxsize"),
    ],
)
def test_out_of_range_flags_raise_value_error(kwargs: dict[str, Any], match: str) -> None:
    with pytest.raises(ValueError, match=match):
        TelegramLoggingHandler(token=OFFLINE_TOKEN, chat_id=OFFLINE_CHAT, validate=False, **kwargs)


def test_unknown_queue_full_policy_fails_fast() -> None:
    with pytest.raises(ValueError, match="unknown queue_full_policy"):
        TelegramLoggingHandler(
            token=OFFLINE_TOKEN,
            chat_id=OFFLINE_CHAT,
            validate=False,
            queue_full_policy="nope",  # type: ignore[arg-type]
        )


@pytest.mark.parametrize("overflow", ["split", "truncate", "drop"])
@pytest.mark.parametrize("parse_mode", [None, "Markdown", "MarkdownV2", "HTML"])
@pytest.mark.parametrize("queue_full_policy", ["block", "drop_newest", "drop_oldest"])
def test_every_flag_combination_constructs_and_closes(
    overflow: str, parse_mode: str | None, queue_full_policy: str
) -> None:
    # Build a handler across the full matrix of enum flags, start its worker,
    # and close it cleanly; the offline construction path must never raise.
    handler = TelegramLoggingHandler(
        token=OFFLINE_TOKEN,
        chat_id=OFFLINE_CHAT,
        level=logging.INFO,
        batch_size=5,
        flush_interval=0.01,
        max_retries=2,
        overflow=overflow,  # type: ignore[arg-type]
        parse_mode=parse_mode,  # type: ignore[arg-type]
        queue_maxsize=100,
        queue_full_policy=queue_full_policy,  # type: ignore[arg-type]
        shutdown_timeout=1.0,
        validate=False,
    )
    try:
        assert isinstance(handler.stats, HandlerStats)
    finally:
        handler.close()
        handler.close()  # idempotent; a second close is a no-op


def test_fresh_stats_snapshot_starts_at_zero() -> None:
    handler = TelegramLoggingHandler(token=OFFLINE_TOKEN, chat_id=OFFLINE_CHAT, validate=False)
    try:
        stats = handler.stats
        assert stats == HandlerStats()
        assert (stats.queued, stats.sent, stats.batches_sent) == (0, 0, 0)
        assert (stats.retries, stats.failed, stats.dropped) == (0, 0, 0)
    finally:
        handler.close()


def test_credentials_resolve_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # token=None / chat_id=None fall back to the env vars (the documented path).
    monkeypatch.setenv("TG_TOKEN", OFFLINE_TOKEN)
    monkeypatch.setenv("TG_CHAT_ID", OFFLINE_CHAT)
    handler = TelegramLoggingHandler(validate=False)
    try:
        assert isinstance(handler.stats, HandlerStats)
    finally:
        handler.close()


# --------------------------------------------------------------------------- #
# Live end-to-end: real token from env, real Telegram delivery (auto-skipped).
# --------------------------------------------------------------------------- #


def _live_handler(**kwargs: Any) -> TelegramLoggingHandler:
    """Build a handler from the real env credentials (token=None -> TG_TOKEN)."""
    return TelegramLoggingHandler(**kwargs)


def _wait_for_stats(
    handler: TelegramLoggingHandler,
    predicate: Callable[[HandlerStats], bool],
    timeout: float = 30.0,
) -> HandlerStats:
    """Poll ``handler.stats`` until ``predicate`` holds.

    ``close()`` returns after ``shutdown_timeout`` even when the worker is
    still mid-send (a daemon thread finishing its last batches in the
    background), so a stats snapshot taken right after close can be
    mid-flight. Live tests must not race the worker: poll until the expected
    counters converge, then assert.
    """
    deadline = time.monotonic() + timeout
    while True:
        stats = handler.stats
        if predicate(stats) or time.monotonic() >= deadline:
            return stats
        time.sleep(0.05)


@requires_live
def test_live_validate_and_deliver_batched_logs() -> None:
    # validate=True does a real getMe at construction; then a batch of records
    # is delivered and the stats counters reflect what actually went out.
    handler = _live_handler(level=logging.INFO, batch_size=5, flush_interval=0.5, validate=True)
    logger = logging.getLogger("tg-smoke.live.batched")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    logger.addHandler(handler)
    try:
        for i in range(5):
            logger.info("live smoke batched record %d/5", i + 1)
        try:
            raise ValueError("intentional smoke-test error")
        except ValueError:
            logger.exception("live smoke record with a traceback")
    finally:
        logger.removeHandler(handler)
        handler.close()  # drains + joins the worker, so all sends complete

    stats = _wait_for_stats(handler, lambda s: s.sent >= 1)
    assert stats.queued >= 6
    assert stats.failed == 0


@requires_live
@pytest.mark.parametrize("parse_mode", ["MarkdownV2", "HTML"])
def test_live_parse_mode_escaping_is_accepted(parse_mode: str) -> None:
    # Log text full of parse-mode specials must be escaped so Telegram accepts
    # it (no 400) rather than trying to parse stray entities.
    handler = _live_handler(parse_mode=parse_mode, flush_interval=0.2, validate=False)
    logger = logging.getLogger(f"tg-smoke.live.{parse_mode}")
    logger.setLevel(logging.WARNING)
    logger.propagate = False
    logger.addHandler(handler)
    try:
        logger.warning("specials _ * [ ] ( ) ~ ` > # + - = | { } . ! < > & %s", "<b>x</b>")
    finally:
        logger.removeHandler(handler)
        handler.close()
    stats = _wait_for_stats(handler, lambda s: s.sent >= 1)
    assert stats.failed == 0


@requires_live
def test_live_split_overflow_delivers_oversized_message() -> None:
    # A single record well over Telegram's 4096-char cap must be split into
    # numbered parts and every part delivered (overflow="split").
    handler = _live_handler(overflow="split", flush_interval=0.5, validate=False)
    logger = logging.getLogger("tg-smoke.live.split")
    logger.setLevel(logging.ERROR)
    logger.propagate = False
    logger.addHandler(handler)
    try:
        logger.error("SPLIT " * 1600)  # ~9600 chars -> multiple parts
    finally:
        logger.removeHandler(handler)
        handler.close()
    stats = _wait_for_stats(handler, lambda s: s.sent >= 1)
    assert stats.sent >= 1
    assert stats.failed == 0


@requires_live
def test_live_batch_size_groups_records_5_and_5() -> None:
    # 10 real error records (each with a traceback) with batch_size=5 must go
    # out as exactly 2 messages (5+5), never as singles or one 10-record
    # message: sent counts records, batches_sent counts messages.
    handler = _live_handler(batch_size=5, flush_interval=0.5, validate=False)
    logger = logging.getLogger("tg-smoke.live.batch-size")
    logger.setLevel(logging.ERROR)
    logger.propagate = False
    logger.addHandler(handler)
    try:
        for i in range(10):
            try:
                raise ValueError(f"intentional smoke error {i + 1}/10")
            except ValueError:
                logger.exception("live smoke batch error %d/10", i + 1)
    finally:
        logger.removeHandler(handler)
        handler.close()
    stats = _wait_for_stats(handler, lambda s: s.sent == 10)
    assert stats.queued == 10
    assert stats.sent == 10
    assert stats.batches_sent == 2
    assert stats.failed == 0
