"""Oversized-message handling: split / truncate / drop (FR-13/14).

Telegram caps a single ``sendMessage`` text at ~4096 characters. When a
formatted batch exceeds that, the configured ``overflow`` policy decides what
goes on the wire:

- ``"split"``  → numbered parts (``(1/3)\\n…``); stripping the one-line header
  from each part and concatenating reconstructs the original text exactly.
- ``"truncate"`` → a single message cut to the limit, ending in a marker.
- ``"drop"`` → nothing is sent (the worker counts the records as ``dropped``).

Input is already escaped for ``parse_mode`` by the worker (see formatting.py),
so this module never re-escapes content. It only needs ``parse_mode`` for two
things (FR-14, best-effort per ARCHITECTURE.md §3.7):

1. Its own part headers (``(1/3)\\n``) and truncation marker contain characters
   that are MarkdownV2/HTML specials, so they are escaped before being glued on.
2. A hard split (a single line longer than the budget, e.g. a giant traceback)
   must not fall between an escape's ``\\`` and the char it escapes, which would
   leave a dangling backslash and un-escape the next special. Because every
   MarkdownV2 escape we emit is a clean two-char ``\\X`` pair, backing the cut up
   over a run of backslashes keeps every pair intact. Newline-preferred cuts are
   always safe (a newline is never mid-escape).
"""

from __future__ import annotations

from .formatting import escape

__all__ = ["prepare_messages", "split_text", "truncate_text"]

# Room reserved at the top of each split part for its ``(i/n)\n`` header. 16
# comfortably covers realistic part counts (e.g. ``(9999/9999)\n`` is 12 chars)
# at the 4096-char boundary, so a part's header + content never exceeds the cap.
_PART_HEADER_RESERVE = 16

# Appended to a truncated message so readers know content was cut (FR-13).
_TRUNCATION_MARKER = "\n… [truncated]"


def prepare_messages(
    text: str, overflow: str, max_length: int, parse_mode: str | None = None
) -> list[str]:
    """Turn one formatted batch into the list of messages to actually send.

    Args:
        text: The formatted batch text (already escaped for ``parse_mode``).
        overflow: One of ``"split"``, ``"truncate"``, ``"drop"``.
        max_length: The per-message character cap (Telegram's ~4096).
        parse_mode: The active parse mode, so headers/markers can be escaped and
            splits avoid cutting an escape sequence (FR-14). ``None`` = plain.

    Returns:
        The messages to send, in order. A message that fits is returned
        unchanged as a single-element list. An empty list means the batch was
        dropped (only possible with ``overflow="drop"`` on oversized text), and
        the caller should count the records as ``dropped``.
    """
    if len(text) <= max_length:
        return [text]
    if overflow == "truncate":
        return [truncate_text(text, max_length, parse_mode)]
    if overflow == "drop":
        return []
    # Default and explicit "split".
    return split_text(text, max_length, parse_mode)


def truncate_text(text: str, max_length: int, parse_mode: str | None = None) -> str:
    """Return ``text`` cut to ``max_length`` chars, ending in a truncation marker.

    If ``text`` already fits it is returned unchanged. Otherwise the result is at
    most ``max_length`` chars and ends with the (parse-mode-escaped) marker. The
    cut is nudged off any trailing escape sequence so a MarkdownV2 ``\\X`` pair is
    never split (FR-14).
    """
    if len(text) <= max_length:
        return text
    marker = escape(_TRUNCATION_MARKER, parse_mode)
    keep = _safe_cut(text, max(max_length - len(marker), 0))
    return text[:keep] + marker


def split_text(text: str, max_length: int, parse_mode: str | None = None) -> list[str]:
    """Split ``text`` into numbered parts, each within ``max_length``.

    Each returned part is a ``"(i/n)\\n"`` header followed by a slice of the
    original. Removing that one-line header from every part and concatenating the
    remainders yields the original ``text`` verbatim. The header is escaped for
    ``parse_mode`` (its parens/slash are MarkdownV2 specials), and its escaped
    length is reserved so header + content stays within ``max_length``.
    """
    if len(text) <= max_length:
        return [text]

    # Reserve room for the (escaped) header. The reserve is a small constant, but
    # escaping can roughly double a header's length in MarkdownV2, so double the
    # reserve for backslash-escaping modes to stay safely under the cap.
    reserve = _PART_HEADER_RESERVE * (2 if parse_mode in ("Markdown", "MarkdownV2") else 1)
    budget = max(max_length - reserve, 1)
    pieces = _split_into_pieces(text, budget)
    total = len(pieces)
    return [
        escape(f"({index}/{total})", parse_mode) + "\n" + piece
        for index, piece in enumerate(pieces, start=1)
    ]


def _split_into_pieces(text: str, budget: int) -> list[str]:
    """Cut ``text`` into consecutive pieces of at most ``budget`` chars.

    Pieces concatenate back to ``text`` exactly. Each cut prefers the last
    newline within the window (kept at the end of the earlier piece so nothing
    is dropped); with no newline available it hard-splits at ``budget``, backed
    off any escape sequence so a ``\\X`` pair is never broken (FR-14).
    """
    pieces: list[str] = []
    start = 0
    length = len(text)
    while start < length:
        end = start + budget
        if end >= length:
            pieces.append(text[start:])
            break
        newline = text.rfind("\n", start, end)
        # Keep the newline with the earlier piece (cut = newline + 1) so the
        # concatenation is byte-for-byte the original; ignore a newline at
        # ``start`` itself, which would make no forward progress.
        cut = newline + 1 if newline > start else _safe_cut(text, end, floor=start + 1)
        pieces.append(text[start:cut])
        start = cut
    return pieces


def _safe_cut(text: str, cut: int, floor: int = 0) -> int:
    """Move ``cut`` left off a run of backslashes so an escape pair stays whole.

    A cut immediately after an odd number of backslashes would split a
    MarkdownV2 ``\\X`` escape. Back up over the trailing backslash run; if that
    would cross ``floor`` (no forward progress), keep the original cut — a
    dangling backslash is the documented best-effort fallback (ARCHITECTURE.md
    §3.7), better than an infinite loop.
    """
    pos = cut
    while pos > floor and text[pos - 1] == "\\":
        pos -= 1
    # Count the backslash run we skipped; if it was odd, the last backslash
    # escapes the char at ``cut`` — drop one more so the pair moves whole to the
    # next piece. Even runs are already balanced (``\\`` = literal backslash).
    if (cut - pos) % 2 == 1 and cut - 1 >= floor:
        return cut - 1
    return cut
