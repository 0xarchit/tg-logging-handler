# Coding Standards — `tg-logging-handler`

Written for coding agents implementing this package. Follow these exactly; deviations should be flagged, not silently made.

## 1. Tooling

- **Formatter/linter**: `ruff` (both format + lint). Config lives in `pyproject.toml`, line length 100.
- **Type checker**: `mypy --strict` must pass with zero errors on the `tg_logging_handler/` package. Tests directory may relax strictness slightly (`disallow_untyped_defs = false` for `tests/`) but should still be typed where practical.
- **Test runner**: `pytest` with `pytest-cov`. Coverage target: ≥90% on `tg_logging_handler/` (excluding `__init__.py` re-exports).
- **Build backend**: `hatchling` or `setuptools` via `pyproject.toml` (no `setup.py`).
- **Pre-commit**: `.pre-commit-config.yaml` running ruff + mypy on every commit; CI re-runs the same checks so local skips don't matter.

## 2. Project Structure Rules

- One responsibility per module (see ARCHITECTURE.md module layout) — do not collapse `worker.py`, `batching.py`, `sender.py` into one file for convenience. Splitting these makes each independently unit-testable without spinning up threads for pure-logic tests (e.g. `batching.py` logic should be testable with fake time, no real `queue.Queue`/threading involved).
- `handler.py` (the public `TelegramLoggingHandler` class) should be thin: constructor wiring + `emit`/`close` delegation. Business logic (batching decisions, retry math, split logic) lives in the other modules, not in the handler class itself. This keeps the `logging.Handler` subclass easy to reason about against the stdlib contract.
- No circular imports. `worker.py` may import from `batching.py`, `sender.py`, `overflow.py`, `formatting.py`, `stats.py`; those modules must not import back from `worker.py` or `handler.py`.

## 3. Style Rules

- All public functions/classes/methods: full type hints, docstrings in Google style (Args/Returns/Raises).
- Prefer `dataclasses` (with `slots=True` where the class is hot-path/high-volume, e.g. any per-record wrapper) over ad-hoc dicts for internal structured data (queue items, batch items).
- No bare `except:` anywhere. Catch specific exception types where feasible; where a broad `except Exception` is genuinely required (worker loop, `emit()`, formatter invocation — see ARCHITECTURE.md §3.2/§4), add an inline comment explaining why it's intentionally broad and confirming it can never propagate.
- No `print()` in library code. Internal diagnostics go to `sys.stderr` directly per the architecture decision in ARCHITECTURE.md §3.5 — not via `logging`.
- No global mutable state. Every `TelegramLoggingHandler` instance owns its own queue, worker thread, HTTP client, and stats — verified by a test that constructs two handlers and confirms independence (see TESTING.md).
- Constants (Telegram's 4096 char limit, default timeouts, etc.) live in one place (`sender.py` or a small `_constants.py`), not duplicated as magic numbers across files.

## 4. Concurrency Rules

- Only `queue.Queue` (or its thread-safety guarantees) may be relied on for producer/consumer safety. Any additional shared mutable state (stats counters) must be protected by an explicit `threading.Lock`, documented at the point of declaration.
- The worker thread must be created with `daemon=True`.
- No use of `threading.Timer` per-record (would create unbounded timer objects under load) — use the blocking-with-timeout pattern on `queue.get(timeout=...)` for the flush-interval trigger instead (see ARCHITECTURE.md §3.4).
- Every code path that can run on the worker thread must be exception-safe — an unhandled exception must never kill the worker thread silently. Wrap the outer loop body in try/except that logs to stderr and continues.

## 5. Error Handling Rules

- Only `TelegramConfigError` is a public exception type users are expected to catch. It is raised only from the constructor / synchronous validation path — never from `emit()`.
- `emit()` must never raise. If anything internal fails during enqueue, call `self.handleError(record)` (the standard library's own escape hatch for handler errors) rather than inventing a new pattern.
- Retry/backoff logic (`sender.py`) must be independently unit-testable with a fake/mocked clock and fake HTTP transport — no real `time.sleep` in tests (use monkeypatched sleep or a small injectable sleep function).

## 6. Documentation Rules

- Every public symbol (`TelegramLoggingHandler`, `HandlerStats`, `TelegramConfigError`) has a complete docstring matching API_SPEC.md.
- `README.md` at repo root is the canonical quickstart — must stay in sync with API_SPEC.md examples; a CI check (or at minimum a manual pre-release checklist item) should confirm README code snippets are still valid against the current API.
- `CHANGELOG.md` follows Keep a Changelog format; every PR that changes public behavior adds an entry.

## 7. Commit / PR Discipline (for agents making incremental commits)

- Small, single-purpose commits mapped to PRD milestones (M0/M1/M2/M3) or individual FR-numbers where practical — makes the history reviewable and bisectable.
- Every commit that touches `tg_logging_handler/` must leave `pytest` and `mypy --strict` green — no "fix in next commit" WIP states on the main branch.
- Commit messages: imperative mood, reference the FR/NFR id from PRD.md when applicable (e.g. `Implement exponential backoff retry (FR-10, FR-11)`).

## 8. What NOT to do

- Do not add a dependency on `python-telegram-bot` or any other full bot framework — this package must stay lightweight (see ARCHITECTURE.md §6).
- Do not implement `asyncio` support in v1 — it's explicitly out of scope (PRD §3, ROADMAP M4).
- Do not add multi-chat / multi-token support to the v1 constructor — out of scope (PRD §10).
- Do not use `logging` for the package's own internal diagnostics (recursion risk — see ARCHITECTURE.md §3.5).
- Do not swallow exceptions silently with no stderr diagnostic and no stats counter increment — every failure path must be observable via `handler.stats` at minimum.
