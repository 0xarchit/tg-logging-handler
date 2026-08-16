<p align="center">
  <img src="assets/logo.svg" alt="tg-logging-handler logo" width="30%">
</p>

# tg-logging-handler

[![CI](https://github.com/0xarchit/tg-logging-handler/actions/workflows/ci.yml/badge.svg)](https://github.com/0xarchit/tg-logging-handler/actions/workflows/ci.yml)
[![CodeQL](https://github.com/0xarchit/tg-logging-handler/actions/workflows/codeql.yml/badge.svg)](https://github.com/0xarchit/tg-logging-handler/actions/workflows/codeql.yml)
[![codecov](https://codecov.io/gh/0xarchit/tg-logging-handler/branch/main/graph/badge.svg)](https://codecov.io/gh/0xarchit/tg-logging-handler)
[![PyPI](https://img.shields.io/pypi/v/tg-logging-handler.svg)](https://pypi.org/project/tg-logging-handler/)
[![Python versions](https://img.shields.io/pypi/pyversions/tg-logging-handler.svg)](https://pypi.org/project/tg-logging-handler/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)


Production-grade Telegram destination for Python's standard `logging` module.

Attach one handler and your logs go to a Telegram chat — asynchronously, so a
slow or down Bot API never blocks your application. Batching, retries with
backoff, 429 handling, oversized-message splitting, bounded-queue back-pressure,
and `parse_mode` escaping are all handled for you.

## Install

```bash
pip install tg-logging-handler
```

Requires Python 3.10+. The only runtime dependency is [`httpx`](https://www.python-httpx.org/).

## Quickstart

```python
import logging
from tg_logging_handler import TelegramLoggingHandler

# Reads TG_TOKEN / TG_CHAT_ID from the environment.
logging.getLogger().addHandler(TelegramLoggingHandler())

logging.error("Something broke")  # delivered to your Telegram chat
```

`TGLoggingHandler` is a shorter alias for the same class.

Provide credentials explicitly instead of via the environment:

```python
handler = TelegramLoggingHandler(token="123456:ABC-DEF", chat_id=-100123456789)
logging.getLogger().addHandler(handler)
```

## Why this handler

- **Never blocks your app.** `emit()` only snapshots the record and enqueues it;
  a single daemon thread does all network I/O. A dead Bot API can never stall a
  request handler.
- **Never crashes your app.** Send failures are retried, then reported to
  `stderr` and counted — never raised into your code. The only exception you can
  catch is `TelegramConfigError`, and only at construction time.
- **No logging recursion.** Internal diagnostics go to `stderr`, never back
  through `logging`.

## Configuration

All constructor arguments after `token` / `chat_id` are keyword-only.

| Argument | Default | Purpose |
|---|---|---|
| `token` | `None` | Bot token; falls back to `TG_TOKEN`. |
| `chat_id` | `None` | Target chat; falls back to `TG_CHAT_ID`. |
| `topic_id` | `None` | Forum topic id; falls back to `TG_TOPIC_ID`. When set on a forum supergroup, messages go to that topic; when unset, to the group's General topic. |
| `level` | `logging.WARNING` | Minimum level (passed to `setLevel`). |
| `batch_size` | `1` | Max records per message (`>= 1`). `1` = send each record immediately. |
| `flush_interval` | `5.0` | Max seconds a partial batch waits (`>= 0`). |
| `max_retries` | `3` | Retry budget for network/5xx errors (`>= 0`). 429s honor `Retry-After` without spending this budget (capped at 10 consecutive waits). |
| `overflow` | `"split"` | Oversized-message policy: `"split"`, `"truncate"`, or `"drop"`. |
| `parse_mode` | `None` | `None`, `"Markdown"`, `"MarkdownV2"`, or `"HTML"`. Formatter output is escaped for the chosen mode. |
| `queue_maxsize` | `10_000` | Bounded queue size. `0` = unbounded (memory risk). |
| `queue_full_policy` | `"drop_newest"` | When the queue is full: `"block"`, `"drop_newest"`, or `"drop_oldest"`. |
| `shutdown_timeout` | `5.0` | Max seconds `close()` waits for the worker to drain. |
| `validate` | `True` | Make a synchronous `getMe` check at construction. Set `False` offline/in tests. |
| `api_base_url` | `"https://api.telegram.org"` | Override for self-hosted Bot API servers. |

### Environment variables

| Variable | Used when |
|---|---|
| `TG_TOKEN` | `token` argument is omitted. |
| `TG_CHAT_ID` | `chat_id` argument is omitted. |
| `TG_TOPIC_ID` | `topic_id` argument is omitted. |

## Batching

Batch records into fewer messages to stay well under Telegram's rate limits:

```python
handler = TelegramLoggingHandler(
    level=logging.ERROR,
    batch_size=10,  # up to 10 records per message
    flush_interval=10.0,  # ...or flush a partial batch after 10s
)
```

A batch is sent when it fills (`batch_size`) or when `flush_interval` elapses,
whichever comes first.

## parse_mode / rich formatting

Set `parse_mode` and the handler escapes each formatted record so log content
(tracebacks, arbitrary user data) can never break Telegram's parser:

```python
handler = TelegramLoggingHandler(parse_mode="MarkdownV2")
```

Because the whole formatter output is escaped, a message like `v1.2_final` is
delivered literally rather than 400-ing the request or rendering as accidental
formatting.

## dictConfig

```python
LOGGING = {
    "version": 1,
    "handlers": {
        "telegram": {
            "()": "tg_logging_handler.TelegramLoggingHandler",
            "level": "ERROR",
            "batch_size": 5,
        },
    },
    "root": {"handlers": ["telegram"], "level": "WARNING"},
}

import logging.config

logging.config.dictConfig(LOGGING)
```

## Inspecting delivery

`handler.stats` returns an immutable snapshot of cumulative counters:

```python
s = handler.stats
print(s.queued, s.sent, s.batches_sent, s.retries, s.failed, s.dropped)
```

## Shutdown

`close()` drains the queue (bounded by `shutdown_timeout`), stops the worker,
and releases the HTTP client. It is idempotent and registered via `atexit`, so
it runs automatically at interpreter exit. For deterministic tests, call it
explicitly or use `logging.shutdown()`.

## Development

```bash
uv sync                          # create .venv + install package + dev deps
uv run pytest                    # run the test suite
uv run ruff check . && uv run mypy   # lint + type-check
```
