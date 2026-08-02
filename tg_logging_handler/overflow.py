"""Oversized-message handling: split / truncate / drop (FR-13/14).

Telegram caps a single ``sendMessage`` text at ~4096 characters. When a
formatted batch exceeds that, the configured ``overflow`` policy decides what
goes on the wire:

- ``"split"``  → numbered parts (``(1/3)\\n…``); stripping the one-line header
  from each part and concatenating reconstructs the original text exactly.
- ``"truncate"`` → a single message cut to the limit, ending in a marker.
- ``"drop"`` → nothing is sent (the worker counts the records as ``dropped``).

Splitting prefers to cut on the last newline before the boundary so lines stay
intact (ARCHITECTURE.md §3.7); when no newline is available in the window it
hard-splits at the boundary. Parts never overlap and always concatenate back to
the source, so no characters are lost or duplicated. Entity-aware splitting for
``parse_mode`` HTML/Markdown is a best-effort concern layered on top in M3; this
module operates on already-escaped plain text.
"""

from __future__ import annotations

__all__ = ["prepare_messages", "split_text", "truncate_text"]

# Room reserved at the top of each split part for its ``(i/n)\n`` header. 16
# comfortably covers realistic part counts (e.g. ``(9999/9999)\n`` is 12 chars)
# at the 4096-char boundary, so a part's header + content never exceeds the cap.
_PART_HEADER_RESERVE = 16

# Appended to a truncated message so readers know content was cut (FR-13).
_TRUNCATION_MARKER = "\n… [truncated]"


def prepare_messages(text: str, overflow: str, max_length: int) -> list[str]:
    """Turn one formatted batch into the list of messages to actually send.

    Args:
        text: The formatted batch text (already escaped for ``parse_mode``).
        overflow: One of ``"split"``, ``"truncate"``, ``"drop"``.
        max_length: The per-message character cap (Telegram's ~4096).

    Returns:
        The messages to send, in order. A message that fits is returned
        unchanged as a single-element list. An empty list means the batch was
        dropped (only possible with ``overflow="drop"`` on oversized text), and
        the caller should count the records as ``dropped``.
    """
    if len(text) <= max_length:
        return [text]
    if overflow == "truncate":
        return [truncate_text(text, max_length)]
    if overflow == "drop":
        return []
    # Default and explicit "split".
    return split_text(text, max_length)


def truncate_text(text: str, max_length: int) -> str:
    """Return ``text`` cut to ``max_length`` chars, ending in a truncation marker.

    If ``text`` already fits it is returned unchanged. Otherwise the result is
    exactly ``max_length`` chars and ends with :data:`_TRUNCATION_MARKER`.
    """
    if len(text) <= max_length:
        return text
    keep = max(max_length - len(_TRUNCATION_MARKER), 0)
    return text[:keep] + _TRUNCATION_MARKER


def split_text(text: str, max_length: int) -> list[str]:
    """Split ``text`` into numbered parts, each within ``max_length``.

    Each returned part is ``"(i/n)\\n"`` followed by a slice of the original.
    Removing that one-line header from every part and concatenating the
    remainders yields the original ``text`` verbatim (no lost or duplicated
    characters). Text that already fits is returned as a single unheadered part.
    """
    if len(text) <= max_length:
        return [text]

    budget = max(max_length - _PART_HEADER_RESERVE, 1)
    pieces = _split_into_pieces(text, budget)
    total = len(pieces)
    return [f"({index}/{total})\n{piece}" for index, piece in enumerate(pieces, start=1)]


def _split_into_pieces(text: str, budget: int) -> list[str]:
    """Cut ``text`` into consecutive pieces of at most ``budget`` chars.

    Pieces concatenate back to ``text`` exactly. Each cut prefers the last
    newline within the window (kept at the end of the earlier piece so nothing
    is dropped); with no newline available it hard-splits at ``budget``.
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
        cut = newline + 1 if newline > start else end
        pieces.append(text[start:cut])
        start = cut
    return pieces
