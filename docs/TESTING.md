# Testing Strategy — `tg-logging-handler`

## 1. Principles

- **100% offline.** No test may make a real network call to Telegram. All HTTP is mocked (`respx` or `pytest-httpx` against the `httpx` client).
- **No real `time.sleep` in unit tests.** Backoff/retry/flush-interval logic must be tested with injected/fake time, so the suite runs in seconds, not minutes.
- **Every FR in PRD.md maps to at least one test.** Use FR ids in test names or docstrings for traceability (e.g. `test_batch_flushes_on_size_fr7`).

## 2. Test Layers

### 2.1 Pure-logic unit tests (no threads, no queue)
- `test_batching.py` — `BatchAccumulator` size/interval trigger logic in isolation, fake clock.
- `test_overflow.py` — split/truncate/drop logic against strings of known length; safe-split boundary behavior for HTML/Markdown (don't split mid-tag); edge cases: exactly 4096 chars, 4097 chars, multi-KB tracebacks.
- `test_formatting.py` — `MessageFormatter` output for each `parse_mode`, including escaping of special characters (`_`, `*`, `` ` ``, `[` for Markdown/MarkdownV2; `<`, `>`, `&` for HTML).
- `test_sender_retry.py` — retry/backoff math (attempt count, delay growth, jitter bounds) with a mocked sleep function and mocked HTTP responses (network error, 500, 429 with `Retry-After` header, permanent 4xx).
- `test_queue_policy.py` — block / drop_newest / drop_oldest behavior against a queue at capacity, without needing the full worker thread (test the policy function directly).

### 2.2 Handler-level integration tests (real thread, mocked HTTP)
- `test_handler.py`:
  - Constructing without token/chat_id and without env vars raises `TelegramConfigError`.
  - Constructing with `validate=True` and a mocked failing `getMe` raises `TelegramConfigError`.
  - Constructing with `validate=False` skips the `getMe` call entirely (assert mock not called).
  - `emit()` returns in well under e.g. 50ms even when the mocked HTTP call is made to hang (proves non-blocking).
  - Two independently constructed handlers have independent stats/queues (no shared global state — CODING_STANDARDS.md §3).
  - `close()` is idempotent (call twice, no error).
  - `close()` drains a queued-but-not-yet-flushed batch within `shutdown_timeout`.
  - Multi-threaded `emit()` from 20 threads × 100 records each results in `stats.sent + stats.dropped + stats.failed == 2000` (no lost, no double-counted records) against a mocked always-succeeds transport.
- `test_worker.py`:
  - Worker thread survives a formatter that raises (record skipped, `failed` incremented, loop continues) — proves ARCHITECTURE.md §4 formatter-failure row.
  - Worker thread survives a sender that raises an unexpected (non-network) exception without dying.

### 2.3 End-to-end scenario tests
- `test_integration.py`, each scenario running the real handler + real worker thread against a mocked Telegram API (`respx` mock covering `sendMessage`/`getMe`):
  1. Single log, no batching → exactly one `sendMessage` call, matches expected text.
  2. `batch_size=5`, 5 quick logs → exactly one `sendMessage` call containing all 5, batched format matches expected shape.
  3. `batch_size=10`, 3 logs then wait past `flush_interval` → exactly one `sendMessage` call with 3 records (interval-triggered flush, not size-triggered).
  4. Oversized single message (>4096 chars) with `overflow="split"` → multiple `sendMessage` calls, numbered parts, concatenated parts (minus part-headers) reconstruct original content.
  5. Oversized message with `overflow="truncate"` → one `sendMessage` call, under length limit, ends with truncation marker.
  6. Oversized message with `overflow="drop"` → zero `sendMessage` calls, `dropped` counter incremented.
  7. Mocked transport returns 500 twice then 200 → 3 `sendMessage` attempts total, `retries == 2`, `sent == 1`, delays observed follow backoff pattern (via fake sleep).
  8. Mocked transport returns 429 with `Retry-After: 2` → handler waits (fake time) ~2s before retrying, this wait does not count against `max_retries`.
  9. Mocked transport always fails → after `max_retries` exhausted, `failed` incremented, no application-thread exception raised, stderr diagnostic emitted (captured via `capsys`).
  10. Queue-full with `queue_full_policy="drop_newest"` under a deliberately slow/blocked mocked transport → newest records dropped, `dropped` counter matches expected count, oldest records still delivered.

## 3. Fixtures (`conftest.py`)

- `mock_transport` — `respx` router pre-wired for `getMe` (success) and `sendMessage` (success), overridable per-test for failure scenarios.
- `fast_handler_factory` — factory fixture producing a `TelegramLoggingHandler` with `validate=False`, tiny `flush_interval`, and the mock transport wired in, so most tests don't repeat constructor boilerplate.
- `capture_stderr` — thin wrapper around `capsys` for asserting internal diagnostic messages.
- `fake_sleep` — monkeypatches the sleep function used internally by `sender.py`'s backoff logic (must be injectable, not a hardcoded `time.sleep` call — see CODING_STANDARDS.md §5) to advance instantly while recording requested durations for assertions.

## 4. CI Requirements

- Matrix: Python 3.10, 3.11, 3.12, 3.13, 3.14 (drop versions as they go EOL; PRD's floor is 3.10 — 3.9 dropped in v0.1.1 to take the pytest ≥9.0.3 security fix, which requires 3.10+).
- Steps: `ruff check`, `ruff format --check`, `mypy --strict tg_logging_handler`, `pytest --cov=tg_logging_handler --cov-report=term-missing --cov-fail-under=90`.
- No network access in the CI test job (optionally enforce with a firewall/hosts-block in CI config as a belt-and-suspenders check that no test accidentally hits the real API).

## 5. Manual / Pre-Release Checklist (not automated, but required before PyPI publish)

- [ ] Run README quickstart snippet against a real throwaway bot/chat once, manually, to confirm real-world behavior (this is the one place real Telegram credentials are acceptable, and only outside the automated suite).
- [ ] Verify `TelegramConfigError` message text is actually helpful (a human unfamiliar with the package should know what to fix).
- [ ] Verify package installs cleanly in a fresh venv from the built wheel (`pip install dist/*.whl`) and the quickstart import works.
- [ ] Confirm `py.typed` is included in the built distribution (check the wheel contents, not just the source tree).
