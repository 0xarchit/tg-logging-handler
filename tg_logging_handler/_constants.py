"""Internal constants shared across modules.

Centralised here so magic numbers are not duplicated across files
(see CODING_STANDARDS.md §3).
"""

from __future__ import annotations

# Telegram's hard cap on a single ``sendMessage`` text payload.
TELEGRAM_MAX_MESSAGE_LENGTH: int = 4096

# Default HTTP timeouts (seconds) for the worker-thread ``httpx.Client``.
DEFAULT_CONNECT_TIMEOUT: float = 5.0
DEFAULT_READ_TIMEOUT: float = 10.0
