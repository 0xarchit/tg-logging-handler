@echo off
(
  uv run mypy || exit /b 1
  uv run ruff format --check . || exit /b 1
  uv run ruff check . || exit /b 1
  uv run pylint tests tg_logging_handler || exit /b 1
  uv run flake8 tests tg_logging_handler || exit /b 1
  uv run pytest --cov=tg_logging_handler || exit /b 1
) > local.log 2>&1
