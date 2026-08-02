# PRD — `tg-logging-handler`

Production-grade Telegram destination for Python's standard `logging` module.

Status: Draft v1.0 — ready for coding-agent implementation
Owner: Archit
Package name (PyPI): `tg-logging-handler` (import name: `tg_logging_handler`) — **verify availability before first publish**; do not use `telegram-logging` (taken, inactive) or `tglogging` (taken).

---

## 1. Problem Statement

Solo developers and small teams self-hosting on a VPS or free-tier box want to know immediately when their application errors out, without paying for or operating an observability stack (Sentry, Datadog, Grafana). Existing PyPI packages that send logs to Telegram (`python-telegram-handler`, `telegram-logging`, `tglogging`, `pyTelegramLogger`, `telegram-bot-logger`, etc.) are each small, single-maintainer projects that implement only a subset of what "production-ready" requires — none combine batching, retry with backoff, rate-limit pacing, and configurable overflow handling in one well-tested package, and most are unmaintained (12+ months without a release).

## 2. Goal

Ship a `logging.Handler` subclass that a developer can add to their existing logging config in one line, that:
- never blocks or slows down the host application,
- never crashes the host application even if Telegram is unreachable or misconfigured,
- never silently loses a log line without an explicit, configured policy saying it's OK to,
- respects Telegram's Bot API limits automatically.

## 3. Non-Goals (v1)

- Not a general-purpose alerting platform (no dashboards, no multi-channel routing beyond Telegram).
- Not an async/`asyncio`-native backend in v1 (thread-based worker only; asyncio backend is a documented future extension point, not built now).
- Not a replacement for `logging` — no monkeypatching, no custom logger classes required.
- No bundled bot-creation / Telegram onboarding UI — docs point to BotFather.
- No deduplication, summary footers, or rich templating in v1 (see ROADMAP.md).

## 4. Target Users

| Segment | Priority | Primary want |
|---|---|---|
| Indie devs / solo self-hosters | Primary | "Ping me on Telegram when prod breaks," zero cost |
| Small teams without observability budget | Primary | Same, plus multi-service / multi-chat routing |
| QA / testers | Secondary | CRITICAL-level pings during test runs |
| Vibecoders / learners | Secondary | Simple mental model, good docs, low friction |
| Prototypers | Non-target | Will likely just use `requests.post` directly; don't over-optimize for them |

## 5. User Stories (v1 scope)

1. As a developer, I add `TelegramLoggingHandler(token=..., chat_id=...)` to my logger and start receiving `ERROR`+ logs in Telegram within seconds, with zero other config.
2. As a developer, I don't want a burst of 50 errors/sec to either flood my Telegram chat or crash my app — the handler batches and paces automatically.
3. As a developer, a single giant traceback should arrive intact (split into parts), not get silently dropped because it exceeds 4096 characters.
4. As a developer, if Telegram is down or rate-limiting me, my application keeps running normally; failed sends are retried with backoff, and if retries are exhausted the log is handled per my configured overflow/queue policy, not by crashing my app.
5. As a developer, I configure everything through constructor args or environment variables (`TG_TOKEN`, `TG_CHAT_ID`) — no separate config file required.
6. As a developer, I get a clear, fail-fast error at startup if my token/chat_id is invalid, not a silent failure at 3am when the first error fires.
7. As a developer, I can run this in a multi-threaded app (Django/FastAPI workers, Celery) without race conditions or duplicate/lost messages.
8. As a developer, I can cleanly shut down (`atexit` and manual `handler.close()`) without losing queued logs or hanging the process.

## 6. Functional Requirements

### 6.1 Core Handler
- FR-1: `TelegramLoggingHandler(logging.Handler)` — standard handler interface (`emit`, `close`, `setLevel`, `setFormatter`).
- FR-2: Constructor accepts `token`, `chat_id`, falls back to `TG_TOKEN`/`TG_CHAT_ID` env vars if omitted.
- FR-3: Startup validation — verify token format and, best-effort, validate via a single lightweight Bot API call (`getMe`); raise `TelegramConfigError` on failure. Validation must be opt-out-able (`validate=False`) for offline/test environments.

### 6.2 Async Delivery
- FR-4: `emit()` never performs network I/O on the calling thread. It enqueues a `LogRecord`-derived item and returns immediately.
- FR-5: A single background worker thread (per handler instance) owns all network calls, batching, retries.
- FR-6: Handler is thread-safe for concurrent `emit()` calls from multiple application threads.

### 6.3 Batching
- FR-7: `batch_size` (default 1) — max records per outgoing message.
- FR-8: `flush_interval` (default 5.0s) — max time a partial batch waits before being sent.
- FR-9: Batch send is triggered by whichever of size/interval happens first.

### 6.4 Retry & Rate Limiting
- FR-10: On send failure (network error, 5xx), retry with exponential backoff + jitter, up to `max_retries` (default 3).
- FR-11: On `429 Too Many Requests`, respect the `retry_after` value from Telegram's response before retrying; this does not count against `max_retries`.
- FR-12: After exhausting retries, apply the configured queue-overflow / failure policy (log a warning to stderr via `logging.getLogger("tg_logging_handler")`, then drop or re-queue per config) — never raise into the application thread.

### 6.5 Message Size / Overflow
- FR-13: Messages exceeding Telegram's ~4096-char limit are handled per `overflow` setting: `"split"` (default, numbered parts), `"truncate"`, or `"drop"`.
- FR-14: Splitting must not break in the middle of a formatting entity when `parse_mode` is HTML/Markdown (best-effort safe-split).

### 6.6 Queue Management
- FR-15: Bounded in-memory queue, `queue_maxsize` (default 10,000). Configurable `queue_full_policy`: `"block"`, `"drop_newest"` (default), `"drop_oldest"`.
- FR-16: Queue and worker must not prevent process exit — worker thread is a daemon thread, plus an `atexit` hook attempts a best-effort final flush with a bounded timeout (`shutdown_timeout`, default 5.0s).

### 6.7 Formatting
- FR-17: Standard `logging.Formatter` support via `setFormatter()`.
- FR-18: `parse_mode` support: `None` (plain), `"Markdown"`, `"MarkdownV2"`, `"HTML"`. Handler must escape user-provided text appropriately for the chosen mode to avoid Telegram API rejection.

### 6.8 Level Filtering
- FR-19: Standard `logging.Handler.setLevel()` / `level=` constructor kwarg. Default `WARNING`.

### 6.9 Observability of the Handler Itself
- FR-20: Handler exposes read-only stats via `handler.stats` (queued, sent, batches_sent, retries, failed, dropped) — in-memory counters, no external dependency.

## 7. Non-Functional Requirements

- NFR-1: Zero required third-party dependencies beyond an HTTP client (`httpx` or `requests` — pick one, document the choice, keep it a single pinned dependency).
- NFR-2: Python 3.9+ support.
- NFR-3: `emit()` call overhead must be sub-millisecond under normal (non-full-queue) conditions.
- NFR-4: 100% of network failure modes must be caught and never propagate as exceptions into application code.
- NFR-5: Test suite must run fully offline (mocked HTTP layer) with no real Telegram credentials required.
- NFR-6: Package must be typed (PEP 561, `py.typed` marker, full type hints, passes `mypy --strict`).

## 8. Success Metrics

- Published to PyPI under a confirmed-available name.
- ≥90% test coverage on core queue/batch/retry logic.
- Zero open "crashes my app" bugs at any point post-release.
- Used in Archit's own projects (ContestSync, AMD hackathon leaderboard cron, etc.) as the first real dogfooding signal — this is the actual acceptance bar for v1, ahead of any download-count vanity metric.

## 9. Milestones (see ROADMAP.md for detail)

1. **M0** — Skeleton package, `TelegramLoggingHandler` sending single messages synchronously-under-the-hood-but-nonblocking-via-thread, no batching. Tests + CI green.
2. **M1** — Batching + retry + rate-limit handling.
3. **M2** — Overflow handling (split/truncate/drop) + queue policies + stats.
4. **M3** — Docs, examples (Django/FastAPI/Celery), PyPI publish, README polish.
5. **M4 (post-v1)** — see ROADMAP.md: dedup, summary footers, asyncio backend, context metadata.

## 10. Open Questions (resolve before/at M0)

- Final package/import name (must confirm PyPI availability).
- HTTP client choice: `httpx` (supports both sync/async, better for future asyncio backend) vs `requests` (more ubiquitous, zero-surprise). Recommendation: `httpx`, sync client only, for v1.
- Single vs multi-chat support in v1 constructor (`chat_id: str | int` vs `chat_ids: list`). Recommendation: single chat only in v1; multi-chat is a documented v2 extension, to keep v1 surface area minimal and reviewable.
