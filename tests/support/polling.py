"""Polling helper: wait for a condition with a timeout instead of a bare sleep."""

import asyncio
from collections.abc import Callable


async def wait_until(
    condition: Callable[[], bool], *, timeout: float = 2.0, interval: float = 0.001
) -> None:
    """Return once ``condition()`` holds; raise ``TimeoutError`` after ``timeout`` seconds."""
    async with asyncio.timeout(timeout):
        while not condition():
            await asyncio.sleep(interval)
