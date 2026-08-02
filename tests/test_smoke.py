"""Smoke test: the package imports cleanly."""

import tg_logging_handler


def test_package_imports() -> None:
    assert tg_logging_handler.__version__ == "0.1.0.dev0"
