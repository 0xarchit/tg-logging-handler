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
        q: queue.Queue[int] = queue.Queue(maxsize=1)
        assert queue_policy.put_drop_newest(q, 1) is True
        assert q.get_nowait() == 1

    def test_put_drop_newest_drops_when_full(self) -> None:
        q: queue.Queue[int] = queue.Queue(maxsize=1)
        q.put_nowait(1)
        assert queue_policy.put_drop_newest(q, 2) is False
        assert q.qsize() == 1
