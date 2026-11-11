"""Simulated monotonic clock: an event loop whose time only moves when it would sleep.

``asyncio.sleep`` / ``asyncio.timeout`` / ``loop.time()`` all read this clock, so the
gateway's real policy numbers (2000 / 500 / 100 ms) run exactly and instantly.
This is the injected "clock and sleeper" for deterministic runs. Timings measured on it
are SIMULATED, never wall-clock.
"""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable

START = 1000.0  # arbitrary non-zero origin, in seconds


class SimulatedLoop(asyncio.SelectorEventLoop):
    def __init__(self) -> None:
        super().__init__()
        self._vtime = START
        real_select = self._selector.select

        def select(timeout: float | None = None):
            if timeout is None:
                raise RuntimeError("simulated loop deadlock: nothing scheduled")
            if timeout > 0:
                self._vtime += timeout
            return real_select(0)

        self._selector.select = select  # type: ignore[method-assign]

    def time(self) -> float:
        return self._vtime


def run_simulated(coro: Awaitable[Any]) -> Any:
    loop = SimulatedLoop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()
