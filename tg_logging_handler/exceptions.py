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


class TelegramSendError(Exception):
    """Internal-only: a ``sendMessage`` attempt failed and has been classified.

    Carries retry classification from the sender back to its retry loop; never
    escapes into application code. ``retryable`` marks transient errors. A
    non-``None`` ``retry_after`` (from a 429) is honored *without* consuming the
    retry budget (FR-11, ARCHITECTURE.md §4).
    """

    def __init__(self, message: str, *, retryable: bool, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.retry_after = retry_after
