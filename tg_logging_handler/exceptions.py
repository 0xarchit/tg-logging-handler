"""Public exceptions for ``tg_logging_handler``."""

from __future__ import annotations


class TelegramConfigError(Exception):
    """Raised at construction for missing/invalid credentials or failed startup validation.

    This is the *only* exception type users should ever need to catch. All
    runtime send failures are handled internally (retried, then reported to
    stderr + counted) and never raised into application code.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message
