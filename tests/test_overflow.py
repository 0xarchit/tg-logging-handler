"""Unit tests for overflow: split / truncate / drop (FR-13/14, TESTING.md §2.1).

Pure-logic against strings of known length, small ``max_length`` for readable
cases plus the real 4096 boundary. The key invariant: split parts, minus their
one-line headers, reconstruct the original text exactly.
"""

from __future__ import annotations

import pytest

from tg_logging_handler._constants import TELEGRAM_MAX_MESSAGE_LENGTH
from tg_logging_handler.overflow import prepare_messages, split_text, truncate_text


def _strip_header(part: str) -> str:
    """Drop the leading ``(i/n)\\n`` header a split part carries."""
    return part.split("\n", 1)[1]


def test_short_text_is_returned_unchanged() -> None:
    assert prepare_messages("hi", "split", 100) == ["hi"]
    assert split_text("hi", 100) == ["hi"]


def test_split_parts_each_within_limit() -> None:
    text = "x" * 250
    parts = split_text(text, 100)
    assert len(parts) >= 3
    assert all(len(p) <= 100 for p in parts)


def test_split_parts_reconstruct_original_no_newlines() -> None:
    text = "abcdefghij" * 30  # 300 chars, no newlines -> hard splits
    parts = split_text(text, 100)
    assert "".join(_strip_header(p) for p in parts) == text


def test_split_prefers_newline_boundaries() -> None:
    # 10 lines of 40 chars each (incl. newline); budget forces multi-line parts
    # but each cut should land on a newline, so no line is broken mid-way.
    lines = [f"{i:02d}-" + "y" * 36 for i in range(10)]
    text = "\n".join(lines)
    parts = split_text(text, 100)
    assert "".join(_strip_header(p) for p in parts) == text
    # No stripped part starts mid-line: every piece begins at a line start.
    bodies = [_strip_header(p) for p in parts]
    assert all(body.split("\n", 1)[0] in text.split("\n") for body in bodies)


def test_split_numbered_headers_are_sequential() -> None:
    parts = split_text("z" * 250, 100)
    total = len(parts)
    assert [p.split("\n", 1)[0] for p in parts] == [f"({i}/{total})" for i in range(1, total + 1)]


def test_split_line_longer_than_budget_hard_splits() -> None:
    # A single 500-char line with no newline still splits and reconstructs.
    text = "q" * 500
    parts = split_text(text, 100)
    assert "".join(_strip_header(p) for p in parts) == text
    assert all(len(p) <= 100 for p in parts)


def test_truncate_marks_and_fits() -> None:
    text = "w" * 500
    out = truncate_text(text, 100)
    assert len(out) == 100
    assert out.endswith("[truncated]")


def test_truncate_leaves_short_text_untouched() -> None:
    assert truncate_text("short", 100) == "short"


def test_drop_returns_no_messages_when_oversized() -> None:
    assert prepare_messages("m" * 500, "drop", 100) == []


def test_drop_keeps_message_that_fits() -> None:
    assert prepare_messages("fits", "drop", 100) == ["fits"]


@pytest.mark.parametrize("size", [4095, 4096, 4097, 20_000])
def test_real_boundary_split_reconstructs(size: int) -> None:
    text = "t" * size
    parts = prepare_messages(text, "split", TELEGRAM_MAX_MESSAGE_LENGTH)
    if size <= TELEGRAM_MAX_MESSAGE_LENGTH:
        assert parts == [text]
    else:
        assert "".join(_strip_header(p) for p in parts) == text
        assert all(len(p) <= TELEGRAM_MAX_MESSAGE_LENGTH for p in parts)
