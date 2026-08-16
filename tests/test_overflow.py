"""Unit tests for overflow: split / truncate / drop.

Pure-logic against strings of known length, small ``max_length`` for readable
cases plus the real 4096 boundary. The key invariant: split parts, minus their
one-line headers, reconstruct the original text exactly.
"""

from __future__ import annotations

import pytest

from tg_logging_handler._constants import TELEGRAM_MAX_MESSAGE_LENGTH
from tg_logging_handler.overflow import _safe_cut, prepare_messages, split_text, truncate_text


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


def _trailing_backslashes(s: str) -> int:
    """Count the run of ``\\`` at the end of ``s`` (parity tells escape safety)."""
    n = 0
    for ch in reversed(s):
        if ch != "\\":
            break
        n += 1
    return n


def test_split_markdownv2_never_dangles_an_escape() -> None:
    # A long single line of escaped dots (\. pairs, no newlines) forces hard
    # splits. No piece may end mid-escape, i.e. with an odd run of backslashes
    # (best-effort safe-split), and parts still reconstruct exactly.
    from tg_logging_handler.formatting import escape

    escaped = escape("." * 500, "MarkdownV2")  # -> r"\.\.\." ...
    parts = split_text(escaped, 100, parse_mode="MarkdownV2")
    bodies = [_strip_header(p) for p in parts]
    assert "".join(bodies) == escaped
    assert all(_trailing_backslashes(b) % 2 == 0 for b in bodies)
    assert all(len(p) <= 100 for p in parts)


def test_split_escapes_its_headers_for_markdownv2() -> None:
    from tg_logging_handler.formatting import escape

    parts = split_text("z" * 250, 100, parse_mode="MarkdownV2")
    total = len(parts)
    for index, part in enumerate(parts, start=1):
        header = part.split("\n", 1)[0]
        assert header == escape(f"({index}/{total})", "MarkdownV2")


def test_truncate_escapes_its_marker_for_markdownv2() -> None:
    from tg_logging_handler.formatting import escape

    out = truncate_text("w" * 500, 100, parse_mode="MarkdownV2")
    assert out.endswith(escape("\n… [truncated]", "MarkdownV2"))
    assert len(out) <= 100


def test_safe_cut_odd_backslash_run_backs_up_one() -> None:
    # A cut right after a lone backslash would split the MarkdownV2 \X pair;
    # the cut must move left by one so the pair moves whole to the next part.
    assert _safe_cut("\\a", 1) == 0


def test_safe_cut_even_backslash_run_keeps_cut() -> None:
    # Two backslashes are a literal backslash (balanced); the cut is fine.
    assert _safe_cut("\\a", 2) == 2


def test_safe_cut_odd_run_respects_floor() -> None:
    # Backing up would cross the floor (no forward progress): keep the cut.
    # r"\a" is a backslash followed by "a" — a plain "\a" would be the bell
    # character and never exercise the backslash run.
    assert _safe_cut(r"\a", 1, floor=1) == 1


def test_split_keeps_markdown_v2_escape_pairs_whole() -> None:
    # A leading character shifts every escape pair to an odd offset, so hard
    # cuts land inside a \_ pair; every pair must stay intact, parts
    # reconstruct byte-for-byte, and no body may end mid-escape.
    text = "x" + r"\_" * 200
    parts = split_text(text, 100, parse_mode="MarkdownV2")
    bodies = [_strip_header(p) for p in parts]
    assert "".join(bodies) == text
    assert all(_trailing_backslashes(b) % 2 == 0 for b in bodies)


def test_truncate_with_tiny_limit_stays_within_limit() -> None:
    # A limit smaller than the marker itself must still clamp the result to
    # the cap; Telegram rejects oversized payloads with a 400.
    assert truncate_text("x" * 100, 5) == "\n… [truncated]"[:5]
    for limit in (0, 5, 20):
        assert len(truncate_text("x" * 100, limit)) <= limit
    assert len(truncate_text("x" * 100, 20, parse_mode="MarkdownV2")) <= 20


@pytest.mark.parametrize("limit", [4, 15])
def test_truncate_markdownv2_tiny_limits_never_dangle_an_escape(limit: int) -> None:
    # Clamping the escaped marker mid-escape would leave a trailing backslash
    # (a dangling MarkdownV2 \X pair 400s the send); the clamp must back off
    # the odd backslash run, stay within the cap, and keep the text budget.
    out = truncate_text("x" * 100, limit, parse_mode="MarkdownV2")
    assert len(out) <= limit
    assert _trailing_backslashes(out) % 2 == 0  # no dangling escape at the end
