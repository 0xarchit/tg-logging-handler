"""Unit tests for the pure pieces: config resolution, stats, queue policy."""

from __future__ import annotations

import queue

import pytest

from tg_logging_handler import queue_policy
from tg_logging_handler.config import resolve_credentials
from tg_logging_handler.exceptions import TelegramConfigError
from tg_logging_handler.stats import HandlerStats, StatsCollector


class TestResolveCredentials:
    def test_explicit_args_win(self) -> None:
        assert resolve_credentials("111:abc", 222) == ("111:abc", "222")

    def test_int_chat_id_is_str(self) -> None:
        assert resolve_credentials("111:abc", -100123) == ("111:abc", "-100123")

    def test_token_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TG_TOKEN", "999:env-tok")
        assert resolve_credentials(None, 1)[0] == "999:env-tok"

    def test_chat_id_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TG_CHAT_ID", "-42")
        assert resolve_credentials("111:abc", None)[1] == "-42"

    def test_missing_token_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("TG_TOKEN", raising=False)
        with pytest.raises(TelegramConfigError):
            resolve_credentials(None, 1)

    def test_missing_chat_id_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("TG_CHAT_ID", raising=False)
        with pytest.raises(TelegramConfigError):
            resolve_credentials("111:abc", None)

    def test_blank_chat_id_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TG_CHAT_ID", "")
        with pytest.raises(TelegramConfigError):
            resolve_credentials("111:abc", None)

    def test_invalid_token_format_raises(self) -> None:
        with pytest.raises(TelegramConfigError):
            resolve_credentials("no-colon-here", 1)

    def test_trailing_newline_token_is_stripped(self) -> None:
        # Tokens read from files/secrets often carry a trailing newline; it
        # must not fail validation or leak into the request URL.
        assert resolve_credentials("111:abc\n", 1) == ("111:abc", "1")

    def test_invalid_short_token_error_is_redacted(self) -> None:
        # A token too short for a safe partial reveal must not appear in the
        # error message at all.
        with pytest.raises(TelegramConfigError) as excinfo:
            resolve_credentials("abc:def", 1)  # non-digit head, 7 chars -> short + invalid
        message = str(excinfo.value)
        assert "abc:def" not in message
        assert "(redacted)" in message

    def test_invalid_long_token_reveals_only_ends(self) -> None:
        # Long tokens may show the first/last 4 chars for diagnosability, but
        # never the middle.
        with pytest.raises(TelegramConfigError) as excinfo:
            # No ':' -> invalid format; 24 chars -> long enough to reveal ends.
            resolve_credentials("AAAAmmmmmmmmmmmmmmmmZZZZ", 1)
        message = str(excinfo.value)
        assert "AAAA" in message and "ZZZZ" in message  # ends shown
        assert "mmmm" not in message  # the middle stays hidden


class TestStats:
    def test_initial_snapshot_is_zero(self) -> None:
        assert StatsCollector().snapshot() == HandlerStats()

    def test_increments_accumulate(self) -> None:
        stats = StatsCollector()
        stats.increment("queued")
        stats.increment("queued")
        stats.increment("failed")
        assert stats.snapshot().queued == 2
        assert stats.snapshot().failed == 1

    def test_snapshot_is_immutable(self) -> None:
        snapshot = StatsCollector().snapshot()
        with pytest.raises(AttributeError):
            snapshot.sent = 99  # type: ignore[misc]  # frozen dataclass

    def test_snapshot_does_not_reflect_later_changes(self) -> None:
        stats = StatsCollector()
        before = stats.snapshot()
        stats.increment("sent")
        assert before.sent == 0
        assert stats.snapshot().sent == 1


class TestQueuePolicy:
    def test_put_drop_newest_succeeds_when_space(self) -> None:
        q: queue.Queue[object] = queue.Queue(maxsize=1)
        result = queue_policy.put_drop_newest(q, 1)
        assert result.enqueued is True
        assert result.dropped == 0
        assert q.get_nowait() == 1

    def test_put_drop_newest_drops_when_full(self) -> None:
        q: queue.Queue[object] = queue.Queue(maxsize=1)
        q.put_nowait(1)
        result = queue_policy.put_drop_newest(q, 2)
        assert result.enqueued is False
        assert result.dropped == 1
        assert q.qsize() == 1
        assert q.get_nowait() == 1  # the older record survives

    def test_put_drop_oldest_evicts_to_make_room(self) -> None:
        q: queue.Queue[object] = queue.Queue(maxsize=2)
        q.put_nowait(1)
        q.put_nowait(2)
        result = queue_policy.put_drop_oldest(q, 3)
        assert result.enqueued is True
        assert result.dropped == 1  # evicted the oldest (1)
        assert list(q.queue) == [2, 3]

    def test_put_drop_oldest_no_eviction_when_space(self) -> None:
        q: queue.Queue[object] = queue.Queue(maxsize=2)
        q.put_nowait(1)
        result = queue_policy.put_drop_oldest(q, 2)
        assert result.enqueued is True
        assert result.dropped == 0

    def test_put_block_enqueues(self) -> None:
        q: queue.Queue[object] = queue.Queue(maxsize=1)
        result = queue_policy.put_block(q, 1)
        assert result.enqueued is True
        assert result.dropped == 0
        assert q.get_nowait() == 1

    def test_select_put_policy_returns_callable(self) -> None:
        for name in ("block", "drop_newest", "drop_oldest"):
            assert callable(queue_policy.select_put_policy(name))

    def test_select_put_policy_rejects_unknown_name(self) -> None:
        with pytest.raises(ValueError, match="unknown queue_full_policy"):
            queue_policy.select_put_policy("drop_middle")
