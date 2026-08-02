# Architecture — `tg-logging-handler`

## 1. Component Overview

```
Application thread(s)
        │  logger.error(...)
        ▼
  TelegramLoggingHandler.emit()          ◄── must be near-instant, never blocks
        │  enqueue LogRecord (or pre-formatted item)
        ▼
   RecordQueue (bounded, thread-safe: queue.Queue)
        │
        ▼
   WorkerThread (single daemon thread, owns all state below)
        │
        ├─► BatchAccumulator   (batch_size / flush_interval logic)
        │
        ├─► MessageFormatter   (LogRecord → text, respects parse_mode)
        │
        ├─► OverflowSplitter   (text → 1..N Telegram-safe chunks)
        │
        ├─► TelegramSender     (HTTP call, retry+backoff, 429 handling)
        │
        └─► StatsCollector     (counters, read by handler.stats)
```

One `TelegramLoggingHandler` instance = one queue = one worker thread = one Telegram chat destination. Multiple handlers (e.g. one per chat, or one for WARNING/one for CRITICAL) can be attached to the same or different loggers independently — no shared global state between instances.

## 2. Module Layout

```
tg_logging_handler/
├── __init__.py          # public API surface: TelegramLoggingHandler, exceptions
├── handler.py            # TelegramLoggingHandler(logging.Handler) — thin, delegates to worker
├── worker.py              # WorkerThread: consumes queue, drives batch/send loop
├── batching.py            # BatchAccumulator: size/interval trigger logic
├── formatting.py          # MessageFormatter, parse-mode escaping
├── overflow.py             # split/truncate/drop logic, safe-split for HTML/Markdown
├── sender.py                # TelegramSender: httpx calls, retry/backoff, 429 handling
├── queue_policy.py           # queue-full policy implementations
├── stats.py                   # HandlerStats dataclass + thread-safe counters
├── exceptions.py                # TelegramConfigError, TelegramSendError (internal only)
├── config.py                     # env var resolution, startup validation (getMe call)
└── py.typed                       # PEP 561 marker
tests/
├── conftest.py            # shared fixtures: mock HTTP transport, fake clock
├── test_handler.py
├── test_worker.py
├── test_batching.py
├── test_overflow.py
├── test_sender_retry.py
├── test_queue_policy.py
├── test_formatting.py
└── test_integration.py     # end-to-end with mocked Telegram API, real thread
```

## 3. Key Design Decisions

### 3.1 Thread, not asyncio, for v1
Rationale: the target user is adding this to arbitrary sync codebases (Flask, Django, scripts, Celery tasks). A background thread + `queue.Queue` requires zero changes to the host app's execution model. An asyncio backend is a real future option (ROADMAP M4) but must not be forced onto v1 users.

### 3.2 `emit()` contract
`emit()` must:
1. Format nothing expensive itself — defer formatting to the worker thread. `emit()` only captures the `LogRecord` (or a lightweight snapshot of the fields it needs, since `LogRecord` objects can be mutated/recycled by some frameworks — copy the needed fields immediately) and calls `queue.put_nowait()` or the configured overflow-safe put.
2. Never raise. Wrap the whole body in `try/except Exception` and route failures through `self.handleError(record)` (the standard `logging.Handler` mechanism) — this is what makes the handler behave like every other stdlib handler under failure.

### 3.3 Queue is the sole hand-off point
No direct thread-to-thread calls beyond the queue. This keeps the concurrency model reviewable: exactly one producer pattern (many app threads → thread-safe queue) and one consumer (the worker). No locks needed beyond what `queue.Queue` already provides internally, except for the stats counters (use `threading.Lock` there, or `itertools.count`-free atomic patterns — keep it simple, a single lock around a small counters dict is fine, it's not a hot path).

### 3.4 Worker loop shape
```python
while not self._stop_event.is_set() or not queue.empty():
    batch = accumulator.collect(queue, timeout=flush_interval)  # blocks up to flush_interval
    if not batch:
        continue
    text_chunks = formatter.format_batch(batch)  # one batch -> one or more messages (overflow)
    for chunk in text_chunks:
        sender.send_with_retry(chunk)  # never raises; internally logs failures via internal logger
```
On `close()`: set `_stop_event`, wait up to `shutdown_timeout` for the worker to drain, then join with timeout. If timeout elapses, log a warning (via the internal `tg_logging_handler` logger, not the user's handler — avoid recursive logging loops) and abandon remaining queue contents.

### 3.5 Avoiding recursive logging loops
The package's own internal diagnostic logging (e.g. "failed to send to Telegram after 3 retries") **must** use a logger (`logging.getLogger("tg_logging_handler")`) that is guaranteed never to also be routed back into this same `TelegramLoggingHandler` instance in the package's own tests/examples. Document this prominently: if a user attaches `TelegramLoggingHandler` to the root logger, internal diagnostics still going through Python's root logger could recurse. Mitigation: the internal logger propagate=False by default, and/or internal diagnostics use `sys.stderr` directly rather than `logging` at all. **Decision: use `sys.stderr` directly for internal failure diagnostics, not the logging module**, to make recursion structurally impossible. This is a load-bearing decision — implement it exactly this way.

### 3.6 HTTP client
`httpx.Client` (sync), one client instance per handler, created lazily in the worker thread (not in `emit()`/constructor thread) to avoid cross-thread client reuse issues. Configure a sane `timeout` (default: connect 5s, read 10s).

### 3.7 Overflow-safe splitting
Plain text: split on the last newline before the 4096-char boundary where possible (avoid mid-line cuts), else hard-split.
HTML/Markdown: track open/close tag or entity balance; never split inside an unclosed `<b>`/`**`/etc. If a safe split point can't be found within a reasonable lookback window, fall back to hard-split and close/reopen the entity around the cut (documented as best-effort, not guaranteed perfect for deeply nested formatting).

## 4. Failure Mode Matrix

| Failure | Behavior |
|---|---|
| Invalid token/chat_id at construction | `TelegramConfigError` raised immediately (fail fast), unless `validate=False` |
| Telegram unreachable (network error) | Retry w/ backoff → exhausted → drop or requeue per policy, stderr diagnostic, counters incremented |
| 429 rate limit | Honor `retry_after`, does not consume retry budget, counters incremented |
| 4xx other than 429 (e.g. bad chat_id discovered post-construction) | Log to stderr, increment `failed`, do not infinite-retry a permanently-broken request |
| Queue full | Per `queue_full_policy`: block (bounded by caller's willingness to block — document this risk clearly), drop_newest, or drop_oldest |
| Process exits abruptly (no clean shutdown) | Daemon thread dies with process; at-most-`flush_interval` seconds of buffered logs may be lost — documented, acceptable for v1 |
| Formatter raises | Caught in worker loop, record skipped, `failed` counter incremented, stderr diagnostic — must not kill the worker thread |

## 5. Public API Surface (v1 — keep minimal)

```python
class TelegramLoggingHandler(logging.Handler):
    def __init__(
        self,
        token: str | None = None,
        chat_id: str | int | None = None,
        level: int | str = logging.WARNING,
        batch_size: int = 1,
        flush_interval: float = 5.0,
        max_retries: int = 3,
        overflow: Literal["split", "truncate", "drop"] = "split",
        parse_mode: Literal[None, "Markdown", "MarkdownV2", "HTML"] = None,
        queue_maxsize: int = 10_000,
        queue_full_policy: Literal["block", "drop_newest", "drop_oldest"] = "drop_newest",
        shutdown_timeout: float = 5.0,
        validate: bool = True,
    ) -> None: ...

    @property
    def stats(self) -> HandlerStats: ...

    def close(self) -> None: ...   # overrides logging.Handler.close, drains + joins worker
```

No other public classes are required for basic usage; `HandlerStats` is the only other exported name (as a typed, read-only dataclass).

## 6. Dependencies

- Runtime: `httpx>=0.27` (pinned with a compatible-release specifier, e.g. `httpx>=0.27,<1.0`).
- Dev: `pytest`, `pytest-cov`, `mypy`, `ruff`, `respx` (httpx mocking) or `pytest-httpx`.
- No dependency on `python-telegram-bot` or any full bot-framework SDK — this is a lightweight, single-purpose handler, not a bot client.
