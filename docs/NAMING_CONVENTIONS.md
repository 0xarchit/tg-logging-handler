# Naming Conventions — `tg-logging-handler`

Locked names. All other docs (PRD, ARCHITECTURE, API_SPEC, CODING_STANDARDS, TESTING, ROADMAP, GITHUB_SETUP) should be updated to match these exactly — treat this file as the single source of truth if any of them still say `tglog-handler` / `tglog_handler` / `TelegramHandler`.

## Locked Names

| Layer | Name |
|---|---|
| PyPI package | `tg-logging-handler` |
| Python module (import name) | `tg_logging_handler` |
| Main class | `TelegramLoggingHandler` |
| Class alias (optional, convenience) | `TGLoggingHandler = TelegramLoggingHandler` |

## Rationale

- **`tg-logging-handler`** — PyPI convention uses hyphens; "logging" (not "logger") because the package subclasses `logging.Handler`, one of several handler types the `logging` module ships with — naming it after the module it plugs into, matching stdlib precedent (`SMTPHandler`, `RotatingFileHandler`).
- **`tg_logging_handler`** — Python import names use underscores (hyphens aren't valid in identifiers); this is the standard PyPI-hyphen → import-underscore mapping.
- **`TelegramLoggingHandler`** — the primary, documented class name. Fully spelled out ("Telegram," not "TG") because in class-level API docs and IDE autocomplete, spelled-out names read clearer than abbreviations — the abbreviation is fine at the package-name level (where brevity matters for `pip install`/typing) but less fine at the class level (where clarity matters more, since it's what appears in stack traces, docs, and autocomplete dropdowns).
- **`TGLoggingHandler`** — kept as a plain alias (not a separate class, not a subclass) for users who prefer the shorter form matching the module name. Zero maintenance cost, purely a convenience import.

## Usage

```python
# Canonical (as documented everywhere)
from tg_logging_handler import TelegramLoggingHandler

handler = TelegramLoggingHandler(token=..., chat_id=...)

# Equivalent, shorter alias — same class, same object
from tg_logging_handler import TGLoggingHandler

handler = TGLoggingHandler(token=..., chat_id=...)
```

In `tg_logging_handler/__init__.py`:

```python
from .handler import TelegramLoggingHandler

TGLoggingHandler = TelegramLoggingHandler

__all__ = ["TelegramLoggingHandler", "TGLoggingHandler", "HandlerStats", "TelegramConfigError"]
```

## Rule for All Other Docs / Code

- Use `TelegramLoggingHandler` in every docstring, every README example, every error message, and every internal reference to "the handler class."
- `TGLoggingHandler` appears **only** in the alias-usage note above and, optionally, one mention in the README ("also available as `TGLoggingHandler` for short") — do not use it as the primary name anywhere else, to avoid the docs referring to "the handler" under two different names inconsistently.
- Internal module/file names stay as already defined in ARCHITECTURE.md (`handler.py`, `worker.py`, etc.) — unaffected by this rename, only the class name inside `handler.py` changes from `TelegramHandler` to `TelegramLoggingHandler`.
