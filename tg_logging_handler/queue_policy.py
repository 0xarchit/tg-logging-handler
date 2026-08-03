"""Queue-full policy implementations (FR-15).

Three behaviors when the bounded queue is at ``queue_maxsize``:

- ``block``       — wait for space (bounded by the caller's willingness to
  block; documented as a back-pressure choice, not the default).
- ``drop_newest`` — discard the incoming record (default; the queue keeps the
  older, already-accepted records).
- ``drop_oldest`` — evict the oldest queued record to make room for the new one.

Each policy is a small pure function over a ``queue.Queue`` so it is unit-testable
against a queue at capacity without spinning up the worker thread
(CODING_STANDARDS.md §2, TESTING.md §2.1). ``put_drop_oldest`` may itself count a
drop (the evicted record), so policies return a :class:`PutResult` rather than a
bare bool.
"""

from __future__ import annotations

import queue
from collections.abc import Callable
from dataclasses import dataclass

__all__ = [
    "PutResult",
    "put_block",
    "put_drop_newest",
    "put_drop_oldest",
    "select_put_policy",
]


@dataclass(frozen=True)
class PutResult:
    """Outcome of a policy put: whether the new item landed, and how many dropped.

    ``dropped`` is the number of records lost by this call — 0 or 1 for the
    drop policies (the incoming record, or the evicted oldest one), always 0 for
    ``block``. The handler adds ``dropped`` to its ``dropped`` counter and, when
    ``enqueued``, its ``queued`` counter (FR-20).
    """

    enqueued: bool
    dropped: int


# A put policy: enqueue ``item`` into ``q``, returning what happened.
PutPolicy = Callable[[queue.Queue[object], object], PutResult]


def put_drop_newest(q: queue.Queue[object], item: object) -> PutResult:
    """Enqueue ``item`` without blocking; drop it if the queue is full."""
    try:
        q.put_nowait(item)
    except queue.Full:
        return PutResult(enqueued=False, dropped=1)
    return PutResult(enqueued=True, dropped=0)


def put_drop_oldest(q: queue.Queue[object], item: object) -> PutResult:
    """Enqueue ``item``, evicting the oldest queued record if the queue is full.

    The eviction + insert is best-effort under concurrency: another producer may
    refill the freed slot first, in which case we retry a bounded number of
    times. Every eviction is counted, and the incoming item is dropped (and
    counted) only if we cannot insert it at all — so the number of records
    actually lost from the queue always matches the reported ``dropped`` count
    (FR-20 accounting). Non-blocking throughout: emit must never block
    (ARCHITECTURE.md §3.2).
    """
    dropped = 0
    for _ in range(_OLDEST_EVICT_ATTEMPTS):
        if _try_put(q, item):
            return PutResult(enqueued=True, dropped=dropped)
        # Queue full: evict the oldest queued record to make room.
        try:
            q.get_nowait()
        except queue.Empty:
            continue  # someone drained it; retry the put
        dropped += 1
    if _try_put(q, item):
        return PutResult(enqueued=True, dropped=dropped)
    return PutResult(enqueued=False, dropped=dropped + 1)


def put_block(q: queue.Queue[object], item: object) -> PutResult:
    """Block until the queue has room, then enqueue ``item`` (never drops)."""
    q.put(item)
    return PutResult(enqueued=True, dropped=0)


def select_put_policy(name: str) -> PutPolicy:
    """Return the put function for a ``queue_full_policy`` name.

    Raises:
        ValueError: If ``name`` is not a known policy (validated at construction
            so a typo fails fast, not silently at first overflow).
    """
    try:
        return _POLICIES[name]
    except KeyError:
        raise ValueError(
            f"unknown queue_full_policy {name!r}; expected one of {sorted(_POLICIES)}"
        ) from None


_OLDEST_EVICT_ATTEMPTS = 3


def _try_put(q: queue.Queue[object], item: object) -> bool:
    try:
        q.put_nowait(item)
    except queue.Full:
        return False
    return True


_POLICIES: dict[str, PutPolicy] = {
    "block": put_block,
    "drop_newest": put_drop_newest,
    "drop_oldest": put_drop_oldest,
}
