"""
Cancellation-safe Single-Flight Concurrency Coordinator.
Ensures concurrent identical cache misses share a single upstream generation.
Core modules MUST NOT import FastAPI or Groq SDK.
"""

import asyncio
from typing import Any, Dict, Tuple


class SingleFlightCoordinator:
    """
    Coordinates concurrent identical requests so that simultaneous misses
    share a single in-flight LLM generation.
    Followers await the leader's future safely via asyncio.shield.
    """

    def __init__(self) -> None:
        self._in_flight: Dict[str, asyncio.Future[Any]] = {}
        self._lock = asyncio.Lock()

    async def register_or_wait(self, fingerprint: str) -> Tuple[bool, asyncio.Future[Any]]:
        """
        Atomically checks or registers in-flight work for this fingerprint.
        Returns (is_leader, future).
        """
        async with self._lock:
            if fingerprint in self._in_flight:
                return False, self._in_flight[fingerprint]

            loop = asyncio.get_running_loop()
            fut: asyncio.Future[Any] = loop.create_future()
            self._in_flight[fingerprint] = fut
            return True, fut

    async def complete_success(self, fingerprint: str, result: Any) -> None:
        """Leader marks the shared future as resolved with success."""
        async with self._lock:
            fut = self._in_flight.pop(fingerprint, None)
            if fut is not None and not fut.done():
                fut.set_result(result)

    async def complete_failure(self, fingerprint: str, exc: BaseException) -> None:
        """
        Leader marks the shared future resolved with exception or cancellation.
        Marks the exception retrieved immediately to prevent
        'Future exception was never retrieved' warnings when zero followers wait.
        """
        async with self._lock:
            fut = self._in_flight.pop(fingerprint, None)
            if fut is not None and not fut.done():
                if isinstance(exc, asyncio.CancelledError):
                    fut.cancel()
                else:
                    fut.set_exception(exc)
                    # Mark exception retrieved to eliminate unretrieved future warning
                    try:
                        fut.exception()
                    except Exception:
                        pass
