# tg-logging-handler

Production-grade Telegram destination for Python's standard `logging` module.

## Status

Under active development — not yet published. The full spec lives in [`docs/`](docs/):

- [PRD](docs/PRD.md) — goals, user stories, functional requirements
- [Architecture](docs/ARCHITECTURE.md) — component design and failure modes
- [API spec](docs/API_SPEC.md) — public API surface
- [Testing strategy](docs/TESTING.md)
- [Roadmap](docs/ROADMAP.md)

## Quickstart (placeholder)

The quickstart below is the intended API and will be verified once the library
lands (milestone M3); see `docs/API_SPEC.md` for the full usage examples.

```python
import logging
from tg_logging_handler import TelegramLoggingHandler

logging.getLogger().addHandler(TelegramLoggingHandler())  # reads TG_TOKEN / TG_CHAT_ID
```

## Development

```bash
uv sync          # create .venv + install package + dev deps
uv run pytest    # run the test suite
```

Naming conventions are locked in [`docs/NAMING_CONVENTIONS.md`](docs/NAMING_CONVENTIONS.md).
