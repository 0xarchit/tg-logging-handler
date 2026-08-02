"""Queue-full policy implementations.

M0 implements ``drop_newest`` only; ``block`` and ``drop_oldest`` arrive in P3
(FR-15). Kept in its own module so each policy is unit-testable against a queue
at capacity without spinning up the worker thread (CODING_STANDARDS.md §2).
"""

from __future__ import annotations

import queue
from typing import TypeVar

__all__ = ["put_drop_newest"]

_T = TypeVar("_T")


def put_drop_newest(q: queue.Queue[_T], item: _T) -> bool:
    """Enqueue ``item`` without blocking; drop it if the queue is full.

    Returns:
        ``True`` if the item was enqueued, ``False`` if it was dropped.
    """
    try:
        q.put_nowait(item)
    except queue.Full:
        return False
    return True
