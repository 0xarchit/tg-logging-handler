"""parse_mode escaping — make formatter output safe for Telegram (FR-17/18).

A ``logging.Formatter`` produces plain text; when a ``parse_mode`` is set,
Telegram parses that text for entities and *rejects the whole request* if it
finds an unbalanced ``*``/``_``/``<``/``&``/etc. Since the text is entirely
log-derived (messages, tracebacks, arbitrary user data), we escape it wholesale
for the chosen mode so no stray character can break parsing. Users who genuinely
want rich formatting supply their own already-formatted text — but the common
case (attach the handler, keep logging) must never 400 because a traceback
contained an underscore.

Escape rules follow Telegram's Bot API "Formatting options" section:

- ``HTML``       → ``&``, ``<``, ``>`` become entities (``&`` first, always).
- ``MarkdownV2`` → 18 reserved chars each get a leading ``\\``.
- ``Markdown``   → legacy mode reserves only ``_ * ` [``; it is officially
  deprecated but still supported, so we escape its smaller set.

``None`` (plain text) needs no escaping and is handled by the caller not calling
here at all.
"""

from __future__ import annotations

__all__ = ["escape"]

# MarkdownV2 reserves these; any literal occurrence must be backslash-escaped
# (Bot API: "Formatting options" → MarkdownV2). Order does not matter because
# each is replaced independently. The backslash IS included: in MarkdownV2 a
# literal ``\`` must be written ``\\``, and escaping it also means every escape
# sequence we emit is a clean two-char ``\X`` pair — which the splitter relies
# on to never cut a message in the middle of an escape (see overflow.py).
_MARKDOWN_V2_SPECIALS = "\\" + r"_*[]()~`>#+-=|{}.!"

# Legacy Markdown only supports these entity delimiters, so only these need
# escaping to avoid accidental formatting.
_MARKDOWN_SPECIALS = r"_*`["


def escape(text: str, parse_mode: str | None) -> str:
    """Return ``text`` escaped so Telegram accepts it under ``parse_mode``.

    Args:
        text: Plain formatter output (message + optional traceback).
        parse_mode: ``None``, ``"HTML"``, ``"Markdown"``, or ``"MarkdownV2"``.

    Returns:
        The escaped text. ``None`` mode returns ``text`` unchanged. An unknown
        mode is treated as plain text (returned unchanged) rather than raising —
        the handler validates the mode at construction, so this is defensive.
    """
    if parse_mode == "HTML":
        # ``&`` must be replaced first, else we would double-escape the ``&`` in
        # the ``&lt;``/``&gt;`` we just inserted.
        return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    if parse_mode == "MarkdownV2":
        return _backslash_escape(text, _MARKDOWN_V2_SPECIALS)
    if parse_mode == "Markdown":
        return _backslash_escape(text, _MARKDOWN_SPECIALS)
    return text


def _backslash_escape(text: str, specials: str) -> str:
    """Prefix every char of ``text`` that is in ``specials`` with a backslash."""
    special_set = set(specials)
    return "".join("\\" + ch if ch in special_set else ch for ch in text)
