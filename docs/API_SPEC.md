# API Spec — `tglog_handler`

## Public Symbols

Exported from `tglog_handler/__init__.py`:

```python
from tglog_handler import TelegramHandler, HandlerStats, TelegramConfigError
```

Everything else in the package is private (prefix internal modules' non-public helpers with `_` where reasonable, but full underscore-prefixing of every internal symbol is not required — module-level privacy via `__init__.py`'s export list is sufficient).

---

## `TelegramHandler`

```python
class TelegramHandler(logging.Handler):
    def __init__(
        self,
        token: str | None = None,
        chat_id: str | int | None = None,
        *,
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
        api_base_url: str = "https://api.telegram.org",
    ) -> None
```

### Parameters

| Name | Type | Default | Notes |
|---|---|---|---|
| `token` | `str \| None` | `None` | Falls back to env `TG_TOKEN`. Raises `TelegramConfigError` if neither provided. |
| `chat_id` | `str \| int \| None` | `None` | Falls back to env `TG_CHAT_ID`. Raises `TelegramConfigError` if neither provided. |
| `level` | `int \| str` | `logging.WARNING` | Passed to `Handler.setLevel`. |
| `batch_size` | `int` | `1` | Must be `>= 1`. `1` = send immediately, no batching. |
| `flush_interval` | `float` | `5.0` | Seconds. Must be `>= 0`. `0` = send as soon as anything is queued (still async). |
| `max_retries` | `int` | `3` | Applies to network/5xx errors only, not 429s (see below). |
| `overflow` | `"split" \| "truncate" \| "drop"` | `"split"` | Behavior when a formatted message exceeds Telegram's ~4096-char cap. |
| `parse_mode` | `None \| "Markdown" \| "MarkdownV2" \| "HTML"` | `None` | Forwarded to Telegram `sendMessage`; handler escapes formatter output accordingly. |
| `queue_maxsize` | `int` | `10000` | Bounded queue size. `0` means unbounded (document the memory risk if a user sets this). |
| `queue_full_policy` | `"block" \| "drop_newest" \| "drop_oldest"` | `"drop_newest"` | Behavior when the queue is at `queue_maxsize`. |
| `shutdown_timeout` | `float` | `5.0` | Max seconds `close()` waits for the worker to drain before abandoning remaining items. |
| `validate` | `bool` | `True` | If `True`, constructor makes a synchronous `getMe` call to verify the token; set `False` in tests/offline environments. |
| `api_base_url` | `str` | `"https://api.telegram.org"` | Override for self-hosted Bot API servers / testing. |

### Raises

- `TelegramConfigError` — missing/invalid token or chat_id, or (if `validate=True`) the `getMe` check fails. Raised synchronously from `__init__`, i.e. fails fast at handler-creation time, not on first log.

### Methods

- `emit(record: logging.LogRecord) -> None` — standard override. Never raises (per `logging.Handler` contract: on internal error it calls `self.handleError(record)`).
- `close() -> None` — standard override. Flushes queue (best-effort, bounded by `shutdown_timeout`), stops worker thread, releases the HTTP client. Safe to call multiple times (idempotent). Also registered via `atexit` automatically so users don't have to call it manually, though explicit `close()`/`logging.shutdown()` is recommended for deterministic tests.
- `stats -> HandlerStats` (property) — current counters snapshot (a copy, not a live-mutating reference).

---

## `HandlerStats`

```python
@dataclass(frozen=True)
class HandlerStats:
    queued: int
    sent: int
    batches_sent: int
    retries: int
    failed: int
    dropped: int
```

All fields are cumulative counts since handler construction. `.stats` returns an immutable snapshot; repeated calls reflect current totals.

---

## `TelegramConfigError`

```python
class TelegramConfigError(Exception):
    """Raised at handler construction time for missing/invalid credentials or failed startup validation."""
```

This is the **only** exception type users should ever need to catch. All runtime send failures are handled internally (retried, then logged to stderr + counted) and never raised into application code.

---

## Environment Variables

| Variable | Purpose |
|---|---|
| `TG_TOKEN` | Bot token, used if `token` constructor arg is omitted. |
| `TG_CHAT_ID` | Chat id, used if `chat_id` constructor arg is omitted. |

---

## Usage Examples (for docs/README, not tests)

### Minimal
```python
import logging
from tglog_handler import TelegramHandler

logging.getLogger().addHandler(TelegramHandler())  # reads TG_TOKEN / TG_CHAT_ID
```

### Explicit config with batching
```python
handler = TelegramHandler(
    token="123:abc",
    chat_id=-100123456789,
    level=logging.ERROR,
    batch_size=10,
    flush_interval=10.0,
    overflow="split",
)
logging.getLogger().addHandler(handler)
```

### dictConfig
```python
LOGGING = {
    "version": 1,
    "handlers": {
        "telegram": {
            "()": "tglog_handler.TelegramHandler",
            "level": "ERROR",
            "batch_size": 5,
        },
    },
    "root": {"handlers": ["telegram"], "level": "WARNING"},
}
```

### Testing / offline
```python
handler = TelegramHandler(token="test", chat_id="1", validate=False, api_base_url="http://localhost:9999")
```

---

## Telegram Bot API Endpoints Used

- `GET /bot{token}/getMe` — startup validation only.
- `POST /bot{token}/sendMessage` — body: `chat_id`, `text`, `parse_mode` (if set), `disable_web_page_preview: true` (always, to keep messages compact).

No other Bot API endpoints are used in v1 (no `sendDocument`, no `editMessageText` for forum-style live-updating messages — that's a documented non-goal, unlike `tglogging`'s `forum_msg_id` feature).

---

## Versioning / Compatibility Policy

- Semantic versioning. Public API = everything listed in this document.
- Constructor kwargs are keyword-only after `token`/`chat_id` (note the `*` in the signature) specifically so new kwargs can be added without breaking positional callers.
- Any change to default values of existing kwargs is a **minor** version bump minimum (behavior change), documented in CHANGELOG.md.
