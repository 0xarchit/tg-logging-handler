"""Internal diagnostics — write straight to ``sys.stderr``, never via ``logging``.

Using ``logging`` here would risk an infinite loop if the user attaches this
handler to the root logger, so recursion is made structurally impossible by
bypassing ``logging`` entirely (ARCHITECTURE.md §3.5 — load-bearing decision).
"""

from __future__ import annotations

import sys

from ._constants import DIAGNOSTIC_PREFIX

__all__ = ["report"]


def report(message: str) -> None:
    """Write a single diagnostic line to ``sys.stderr``.

    ``sys.stderr`` is looked up at call time (not import time) so that test
    fixtures capturing stderr see the redirected stream.
    """
    print(f"[{DIAGNOSTIC_PREFIX}] {message}", file=sys.stderr)
