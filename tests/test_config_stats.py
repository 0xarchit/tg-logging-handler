"""Unit tests for the pure pieces: config resolution, stats, queue policy."""

from __future__ import annotations

import queue
from typing import cast

import pytest

from tg_logging_handler import queue_policy
from tg_logging_handler.config import resolve_credentials, resolve_topic_id
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

    def test_env_chat_id_trailing_newline_is_stripped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Same story for chats: .env files routinely end values with a
        # newline, and an unstripped one would 400 at send time.
        monkeypatch.setenv("TG_CHAT_ID", "-42\n")
        assert resolve_credentials("111:abc", None)[1] == "-42"

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


class TestResolveTopicId:
    def test_explicit_int_passes_through(self) -> None:
        assert resolve_topic_id(42) == 42

    def test_none_returns_none_without_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("TG_TOPIC_ID", raising=False)
        assert resolve_topic_id(None) is None

    def test_env_string_is_parsed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TG_TOPIC_ID", "42")
        assert resolve_topic_id(None) == 42

    def test_explicit_arg_wins_over_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TG_TOPIC_ID", "7")
        assert resolve_topic_id(42) == 42

    def test_empty_env_is_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TG_TOPIC_ID", "")
        assert resolve_topic_id(None) is None

    def test_invalid_env_string_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TG_TOPIC_ID", "not-a-number")
        with pytest.raises(ValueError, match="TG_TOPIC_ID must be an integer"):
            resolve_topic_id(None)

    @pytest.mark.parametrize("bad", [True, False, 3.5, "42"])
    def test_non_int_explicit_values_are_rejected(self, bad: object) -> None:
        # bool is an int subclass, floats compare fine, and str would crash on
        # the range comparison; all must fail with a clean ValueError.
        with pytest.raises(ValueError, match="topic_id must be an integer"):
            resolve_topic_id(bad)  # type: ignore[arg-type]  # deliberate misuse

    def test_rejection_message_names_the_type(self) -> None:
        with pytest.raises(ValueError, match="got bool"):
            resolve_topic_id(True)

    def test_non_positive_is_rejected(self) -> None:
        for bad in (0, -1, -100):
            with pytest.raises(ValueError, match="positive"):
                resolve_topic_id(bad)


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


class _ScriptedQueue:
    """Duck-typed stand-in for ``queue.Queue`` with per-call scripted outcomes.

    Only the two methods ``put_drop_oldest`` touches are provided. ``puts``
    entries: ``"full"`` makes ``put_nowait`` raise ``queue.Full``, anything
    else succeeds. ``gets`` entries: ``"empty"`` makes ``get_nowait`` raise
    ``queue.Empty``, anything else returns an evictable item. This lets tests
    exercise the concurrency-race branches that a real queue can never reach
    single-threaded.
    """

    def __init__(self, puts: list[str], gets: list[str]) -> None:
        self._puts = iter(puts)
        self._gets = iter(gets)

    def put_nowait(self, item: object) -> None:
        if next(self._puts) == "full":
            raise queue.Full

    def get_nowait(self) -> object:
        if next(self._gets) == "empty":
            raise queue.Empty
        return "evicted"

    def task_done(self) -> None:
        pass


class TestValidateToken:
    def test_non_json_200_body_is_config_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A proxy/gateway can answer 200 with an HTML error page; the docs
        # promise TelegramConfigError, never a bare ValueError.
        import httpx

        from tg_logging_handler.config import validate_token

        monkeypatch.setattr(
            httpx,
            "get",
            lambda *args, **kwargs: httpx.Response(200, text="<html>gateway error</html>"),
        )
        with pytest.raises(TelegramConfigError, match="non-JSON"):
            validate_token("111:abc", "https://api.telegram.org")

    def test_network_error_is_config_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # An unreachable API (offline host, DNS failure) must surface as the
        # documented TelegramConfigError, with the offline-escape hatch hint.
        import httpx

        from tg_logging_handler.config import validate_token

        def offline(*args: object, **kwargs: object) -> httpx.Response:
            raise httpx.ConnectError("offline")

        monkeypatch.setattr(httpx, "get", offline)
        with pytest.raises(TelegramConfigError, match="Could not reach Telegram"):
            validate_token("111:abc", "https://api.telegram.org")


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

    def test_put_drop_oldest_task_dones_evicted_items(self) -> None:
        # Every eviction is a get(); without a matching task_done(),
        # queue.join() would hang forever on a full-and-drained queue.
        q: queue.Queue[object] = queue.Queue(maxsize=2)
        q.put_nowait(1)
        q.put_nowait(2)
        queue_policy.put_drop_oldest(q, 3)
        # Only items still in the queue may lack a task_done().
        assert q.unfinished_tasks == q.qsize() == 2

    def test_put_drop_oldest_no_eviction_when_space(self) -> None:
        q: queue.Queue[object] = queue.Queue(maxsize=2)
        q.put_nowait(1)
        result = queue_policy.put_drop_oldest(q, 2)
        assert result.enqueued is True
        assert result.dropped == 0

    def test_put_drop_oldest_handles_queue_draining_between_evict_and_retry(
        self,
    ) -> None:
        # Race path: between a failed put and the eviction get, another
        # producer drains the queue, so get_nowait raises Empty and the loop
        # must retry instead of crashing. Scripted via a fake queue.
        fake = cast(
            "queue.Queue[object]",
            _ScriptedQueue(puts=["full", "full", "ok"], gets=["empty", "item"]),
        )
        result = queue_policy.put_drop_oldest(fake, "new")
        assert result.enqueued is True
        assert result.dropped == 1  # one real eviction happened, then room appeared

    def test_put_drop_oldest_gives_up_when_slot_keeps_refilling(self) -> None:
        # Race path: the freed slot is instantly refilled by a competing
        # producer on every attempt, so all evictions fail and the incoming
        # item is dropped (and counted) after the bounded attempt budget.
        fake = cast(
            "queue.Queue[object]",
            _ScriptedQueue(puts=["full", "full", "full", "full"], gets=["item"] * 3),
        )
        result = queue_policy.put_drop_oldest(fake, "new")
        assert result.enqueued is False
        assert result.dropped == 4  # 3 evicted victims + the incoming record

    def test_put_drop_oldest_lands_item_after_attempt_budget(self) -> None:
        # Race path: the slot keeps refilling for all three attempts, but the
        # final (post-budget) put succeeds, so the item lands but the victims
        # are still counted.
        fake = cast(
            "queue.Queue[object]",
            _ScriptedQueue(puts=["full", "full", "full", "ok"], gets=["item"] * 3),
        )
        result = queue_policy.put_drop_oldest(fake, "new")
        assert result.enqueued is True
        assert result.dropped == 3

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
