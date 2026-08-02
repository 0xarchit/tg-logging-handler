# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
