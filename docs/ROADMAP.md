# Roadmap — `tglog-handler`

## M0 — Skeleton (target: first green CI)
- Package scaffold (`pyproject.toml`, module layout per ARCHITECTURE.md §2).
- `TelegramHandler` with constructor validation (FR-1/2/3), `TelegramConfigError`.
- Worker thread + queue wiring, no batching (`batch_size=1` path only), single-message send via `sender.py`, no retry yet.
- Basic test suite for the above (subset of TESTING.md §2.2/2.3 scenario 1).
- CI pipeline green (ruff, mypy, pytest) per TESTING.md §4.
- **Exit criteria**: `logger.error("x")` reliably produces exactly one Telegram message in an integration test, `emit()` proven non-blocking.

## M1 — Batching, Retry, Rate Limiting
- `BatchAccumulator` (FR-7/8/9).
- `TelegramSender` retry/backoff (FR-10), 429/`Retry-After` handling (FR-11).
- Failure-path stats + stderr diagnostics (FR-12).
- Test scenarios 2, 3, 7, 8, 9 from TESTING.md §2.3.
- **Exit criteria**: burst of 100 rapid logs under `batch_size=10` produces the expected number of batched messages with no application-thread impact; simulated Telegram outage degrades gracefully.

## M2 — Overflow & Queue Policies
- `overflow.py` split/truncate/drop (FR-13/14).
- `queue_policy.py` block/drop_newest/drop_oldest (FR-15), bounded queue + daemon thread + `atexit` (FR-16).
- `HandlerStats` full implementation (FR-20).
- Test scenarios 4, 5, 6, 10 from TESTING.md §2.3.
- **Exit criteria**: oversized tracebacks and queue-saturation scenarios behave exactly per the failure-mode matrix in ARCHITECTURE.md §4, with no lost data outside explicitly configured drop policies.

## M3 — Formatting, Docs, Publish
- `parse_mode` support + escaping (FR-17/18).
- README with quickstart, dictConfig example, Django/FastAPI/Celery usage snippets.
- CHANGELOG.md initialized, version 0.1.0 tagged.
- Manual pre-release checklist (TESTING.md §5) completed.
- Confirm final package name availability on PyPI, publish.
- **Exit criteria**: `pip install tglog-handler` (or chosen final name) works from a clean environment; README examples verified against the real package.

## M4 — Post-v1 (not committed, prioritize based on real usage/issues)
Ordered roughly by expected value based on the competitive analysis (none of these exist well-implemented in any competitor today):
1. **Deduplication** — collapse repeated identical messages within a time window into a "repeated ×N" summary. Highest differentiator; directly solves noisy-error-loop spam, which is the #1 complaint pattern in this space.
2. **Delivery statistics export** — optional Prometheus-style text exposition or a `stats.as_dict()` convenience for users wiring their own monitoring, beyond the current in-memory-only `handler.stats`.
3. **Summary footer / batch headers** — small UX polish for high-volume batches (e.g. "🚨 18 logs — Errors: 6, Warnings: 8...").
4. **Context metadata** (hostname, pid, module, line number) — valuable for distributed/multi-service setups; opt-in via a constructor flag to avoid bloating default message size.
5. **Multi-chat support** — send the same handler's output to multiple `chat_id`s (or route by level to different chats). Deferred from v1 deliberately (PRD §10) to keep the initial surface area small; revisit once v1 API has stabilized and real usage confirms demand.
6. **AsyncIO backend** — optional `AsyncTelegramHandler` or an internal async worker mode for apps already running an event loop end-to-end, avoiding a redundant thread. Only pursue if there's a concrete request — thread-based v1 already satisfies the sync-codebase majority of the target audience.

## Explicitly Not Planned
- Rich HTML/emoji templating engine — out of scope indefinitely; users can achieve this via `parse_mode="HTML"` and their own formatter.
- Bundled bot-setup CLI/wizard — docs link to BotFather instructions instead.
- Support for logging backends other than Telegram (Discord/Slack) — a different package if ever pursued, not a feature of this one.
