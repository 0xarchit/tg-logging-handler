"""Unit tests for parse_mode escaping (FR-18, TESTING.md §2.1).

Pure-logic: escape() output for each mode, covering the special-char sets and
the "unchanged when nothing special" and None-passthrough cases.
"""

from __future__ import annotations

import pytest

from tg_logging_handler.formatting import escape


def test_none_mode_is_passthrough() -> None:
    text = "a_b*c`d[e<f>g&h"
    assert escape(text, None) == text


def test_unknown_mode_is_passthrough() -> None:
    # Defensive: the handler validates the mode, so an unknown one just passes.
    text = "a_b*c"
    assert escape(text, "Rst") == text


def test_html_escapes_amp_lt_gt() -> None:
    assert escape("a & b < c > d", "HTML") == "a &amp; b &lt; c &gt; d"


def test_html_escapes_amp_first_no_double_escape() -> None:
    # A literal "<" must become "&lt;", not "&amp;lt;".
    assert escape("<", "HTML") == "&lt;"
    assert escape("&lt;", "HTML") == "&amp;lt;"


def test_html_leaves_markdown_specials_alone() -> None:
    assert escape("_*`[", "HTML") == "_*`["


@pytest.mark.parametrize("ch", list(r"_*[]()~`>#+-=|{}.!"))
def test_markdownv2_escapes_each_special(ch: str) -> None:
    assert escape(ch, "MarkdownV2") == "\\" + ch


def test_markdownv2_escapes_backslash() -> None:
    assert escape("\\", "MarkdownV2") == "\\\\"


def test_markdownv2_leaves_plain_text_alone() -> None:
    assert escape("hello world 123", "MarkdownV2") == "hello world 123"


def test_markdownv2_mixed() -> None:
    assert escape("v1.2", "MarkdownV2") == "v1\\.2"
    assert escape("a_b", "MarkdownV2") == "a\\_b"


@pytest.mark.parametrize("ch", list("_*`["))
def test_legacy_markdown_escapes_its_four(ch: str) -> None:
    assert escape(ch, "Markdown") == "\\" + ch


def test_legacy_markdown_leaves_v2_only_specials_alone() -> None:
    # Legacy Markdown has a smaller reserved set: these are NOT escaped.
    assert escape(".!()#", "Markdown") == ".!()#"


def test_legacy_markdown_does_not_escape_backslash() -> None:
    assert escape("\\", "Markdown") == "\\"
