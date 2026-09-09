"""State-based waiting primitives. No unconditional sleeps as a synchronization mechanism."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable


class Deadline:
    """A monotonic deadline that can be shared across nested waits."""

    def __init__(self, timeout_s: float) -> None:
        self._end = time.monotonic() + max(timeout_s, 0.0)

    def remaining(self) -> float:
        return max(self._end - time.monotonic(), 0.0)

    def expired(self) -> bool:
        return self.remaining() <= 0.0


async def wait_until(
    predicate: Callable[[], Awaitable[bool]],
    *,
    timeout_s: float,
    poll_interval_s: float,
) -> bool:
    """Poll ``predicate`` until true or the timeout elapses. Evaluates at least once."""
    deadline = Deadline(timeout_s)
    while True:
        if await predicate():
            return True
        if deadline.expired():
            return False
        await asyncio.sleep(min(poll_interval_s, max(deadline.remaining(), 0.01)))
