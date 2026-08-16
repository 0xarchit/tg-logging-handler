@echo off
(
  uv run mypy
  uv run ruff format --check .
  uv run ruff check .
  uv run pylint tests
  uv run pylint tg_logging_handler
  uv run flake8 tests
  uv run flake8 tg_logging_handler
  uv run pytest --cov=tg_logging_handler
) > local.log 2>&1
