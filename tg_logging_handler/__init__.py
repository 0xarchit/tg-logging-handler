"""Production-grade Telegram destination for Python's standard ``logging`` module."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

from .exceptions import TelegramConfigError
from .handler import TelegramLoggingHandler
from .stats import HandlerStats

# Short convenience alias; same class, same object.
TGLoggingHandler = TelegramLoggingHandler

try:
    # Single source of truth: the version declared in pyproject.toml, read from
    # installed metadata. Never hardcode it here; that only drifts.
    __version__ = version("tg-logging-handler")
except PackageNotFoundError:  # pragma: no cover - only when imported uninstalled
    __version__ = "0+unknown"

__all__ = [
    "HandlerStats",
    "TGLoggingHandler",
    "TelegramConfigError",
    "TelegramLoggingHandler",
    "__version__",
]
