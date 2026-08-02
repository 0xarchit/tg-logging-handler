"""Production-grade Telegram destination for Python's standard ``logging`` module."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

from .exceptions import TelegramConfigError
from .handler import TelegramLoggingHandler
from .stats import HandlerStats

# Short convenience alias — same class, same object (NAMING_CONVENTIONS.md).
TGLoggingHandler = TelegramLoggingHandler

try:
    __version__ = version("tg-logging-handler")
except PackageNotFoundError:  # pragma: no cover - only when imported uninstalled
    __version__ = "0.1.0.dev0"

__all__ = [
    "HandlerStats",
    "TGLoggingHandler",
    "TelegramConfigError",
    "TelegramLoggingHandler",
    "__version__",
]
