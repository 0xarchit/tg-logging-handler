# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.4] - 2026-08-17

### Added

- New `rate_limited` counter in `HandlerStats`: 429 `Retry-After` waits are
  now counted even though they never consume the retry budget, so a
  rate-limit storm shows up in the stats instead of reporting zero retries.

### Fixed

- Failed handler construction no longer leaves a half-built handler registered
  with the logging module. Previously, exit-time `logging.shutdown()` called
  `close()` on the half-built instance and printed a stray `AttributeError`
  after the real construction error; the registration is now dropped on
  failure.
- Worker shutdown is event-backed: a saturated queue or a `drop_oldest`
  eviction can no longer lose the shutdown sentinel and leak the worker
  thread (and its HTTP client) after `close()`.
- **Shutdown no longer waits out a long `flush_interval`**: the accumulator
  polls the shutdown event between capped waits, so `close()` returns promptly
  (≤ ~1s) even with a partial batch pending and the sentinel lost; the pending
  batch is still flushed first.
- A close() that interleaves with an in-flight `emit()` no longer orphans the
  record: `emit` and `close` are mutually exclusive, so nothing accepted
  before `close()` is lost (and `queue_full_policy="block"` can no longer hang
  an emitter past shutdown).
- Post-close `emit()` drops and counts the record instead of potentially
  hanging the caller under `queue_full_policy="block"`.
- `truncate_text` clamps the marker when the limit is smaller than the marker
  itself, so a truncated message can never exceed the cap (Telegram 400s
  oversized payloads).
- The one-time 429 heads-up notification now swallows any failure (not just
  transport errors), so it can never surface through `send_with_retry`.
- **Batches are no longer split by a pending shutdown.** The shutdown poll now
  runs only after a capped wait elapsed with nothing arriving, so records that
  keep flowing still group into one batch even when `close()` was called during
  the drain (previously every remaining record flushed as its own message).
- A non-200 `sendMessage` response (a 3xx — redirects are never followed — or
  a non-200 2xx) is no longer reported as delivered; getMe validation
  maps non-JSON 200 bodies to `TelegramConfigError` instead of a bare
  `ValueError`; env `TG_CHAT_ID` trailing whitespace is stripped like the
  token's; queue `task_done` accounting is now exact, so `queue.join()` can
  no longer hang.
- `close()` now enqueues the shutdown sentinel **before** signaling the
  worker, so the worker can never exit while the sentinel is still about to be
  queued and strand it in the queue (a user's `queue.join()` would hang).
- getMe validation requires an object body with `ok == True` exactly: a
  scalar/list JSON body or a merely truthy `ok` (e.g. `1`) maps to
  `TelegramConfigError` instead of a raw `AttributeError` or a false accept.
- The clamped truncation marker is nudged off a trailing backslash, so a tiny
  `max_length` with MarkdownV2 can never leave a dangling escape pair.
- HTTP redirects are disabled on the send client: a 302/308 with a `Location`
  is never followed, so a redirected `sendMessage` request can never be
  mistaken for a delivery.

## [0.1.3] - 2026-08-16

### Added

- **Forum topic support.** New `topic_id` argument (keyword-only, defaults to
  `None`) and `TG_TOPIC_ID` environment variable. When set and the target is a
  group with topics enabled (or a forum supergroup), every message goes to that
  topic via the Bot API `message_thread_id` field; when unset, payloads are
  unchanged and messages go to the group directly (or the General topic). The
  one-time 429 heads-up notice is posted into the same topic, never General.
  Backward compatible: existing handlers need no changes.
- Runtime type check for `topic_id`: explicit non-integer values (`bool`,
  `float`, `str`) are rejected with `ValueError` at construction instead of
  passing through and failing later or corrupting the payload.

## [0.1.2] - 2026-08-15

### Fixed

- **Worker shutdown could hang**: when shutdown arrived while a batch was
  being drained, the loop could return to collecting on a permanently-empty
  queue instead of stopping — it now stops after flushing the in-flight
  batch (`WorkerThread._loop`).
- Corrected the mypy typing of the fake sender in the worker shutdown test.

### Changed

- **Code-review pass over comments and docstrings** (PR #3): removed stale
  references to the deleted `docs/` tree from source and tests, and corrected
  the docs that drifted from the implementation:
  - Documented the worker's failed-count accounting explicitly: batch-
    processing exceptions increment `failed` once, send failures
    (`SendOutcome(delivered=False)` — retries exhausted, permanent failure,
    or `_MAX_RATE_LIMIT_WAITS` consecutive 429 waits even when the retry
    budget remains) increment `failed` by `len(batch)`, and `overflow="drop"`
    increments `dropped` by `len(batch)`.
  - Scoped the module docstring's exception-safety claim to the exceptions
    actually caught (`_process_batch`) and to batches that reach the sender.
  - Dropped the unsupported "the sender already reported the cause" comment;
    the sender returns `SendOutcome` and the worker logs the drop.
- **Docs cleanup**: `docs/` is no longer tracked (kept locally, gitignored);
  README drops the `docs/` links and the License section and gains the new
  banner/logo assets.
- **CI**: publish workflows pin the smoke test to the just-published version
  so the release never asserts a stale version.

## [0.1.1] - 2026-08-03

### Changed

- **Minimum Python is now 3.10** (was 3.9). Python 3.9 reached end-of-life in
  October 2025, and the pytest security fix below requires 3.10+. CI, publish
  smoke tests, and classifiers updated accordingly.
- `__version__` now derives solely from installed package metadata
  (`importlib.metadata`); the uninstalled-source fallback is the non-real
  sentinel `0+unknown` instead of a hardcoded release string, so the version can
  never drift out of sync with `pyproject.toml`.

### Security

- Bumped the dev/test toolchain floor to `pytest>=9.0.3`, picking up the fix for
  the world-readable `/tmp/pytest-of-*` temp-dir advisory (local DoS / possible
  privilege escalation on multi-user UNIX hosts). Test-only — never shipped to
  users; the sole runtime dependency remains `httpx`.

### Fixed

- Removed the redundant `import tg_logging_handler` alongside
  `from tg_logging_handler import …` in the test suite, clearing the CodeQL
  `py/import-and-import-from` findings.

## [0.1.0] - 2026-08-03

### Added

- **M0 — Non-blocking handler skeleton.** `TelegramLoggingHandler` (alias
  `TGLoggingHandler`) with a bounded queue and a single daemon worker thread;
  `emit()` snapshots the record and enqueues without ever blocking or raising.
  Synchronous `getMe` token validation at construction (`validate=True`),
  credentials from args or `TG_TOKEN`/`TG_CHAT_ID` env vars, `close()` with
  `shutdown_timeout` drain, `atexit` cleanup.
- **M1 — Batching, retry, rate limiting.** Size/interval batch flushing
  (`batch_size`, `flush_interval`); exponential-backoff retries with jitter for
  network/5xx failures (`max_retries`); 429 handling with `Retry-After`
  respect (capped at 10 consecutive waits so a stuck 429 can't spin the worker
  forever, and a non-numeric HTTP-date `Retry-After` falls back to 1s);
  `disable_web_page_preview` on every message.
- **M2 — Overflow handling, queue policies, full stats.**
  - Oversized-message policies (Telegram caps `sendMessage` at ~4096 chars):
    `split` (numbered `(1/3)` parts that reconstruct the original exactly),
    `truncate` (single message + `… [truncated]` marker), `drop` (nothing sent,
    counted) — configurable via `overflow`.
  - Queue-full policies: `block`, `drop_newest` (default), `drop_oldest`;
    every lost record is counted (FR-20 accounting).
  - `HandlerStats` snapshot: `queued`, `sent`, `batches_sent`, `retries`,
    `failed`, `dropped`.
- **M3 — `parse_mode` escaping.** `escape()` in `formatting.py` escapes
  formatter output for `None`/`Markdown`/`MarkdownV2`/`HTML` so log content can
  never break Telegram's parser. Split/truncate are parse-mode-aware: escaped
  `(i/n)` headers and truncation marker, and hard splits never cut a `\X`
  escape pair (best-effort per ARCHITECTURE.md §3.7).
- Package scaffold: hatchling `pyproject.toml` (uv-managed dev groups), `py.typed`
  marker, `TelegramConfigError` as the only user-facing exception, test suite
  (pytest + respx, 100% offline) with ≥90% coverage gate.
