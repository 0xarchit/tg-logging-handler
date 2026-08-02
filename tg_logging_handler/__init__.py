"""Production-grade Telegram destination for Python's standard ``logging`` module."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("tg-logging-handler")
except PackageNotFoundError:  # pragma: no cover - only when imported uninstalled
    __version__ = "0.1.0.dev0"

__all__: list[str] = []
