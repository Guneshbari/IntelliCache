"""
Cancellation-safe Single-Flight Concurrency Coordinator.
Ensures concurrent identical cache misses share a single upstream generation within the same namespace.
Core modules MUST NOT import FastAPI or Groq SDK.
"""

import asyncio
from typing import Any, Dict, Hashable, Tuple, Union

FlightKey = Union[Tuple[str, str], str, Hashable]


class SingleFlightCoordinator:
    """
    Coordinates concurrent identical requests so that simultaneous misses within
    the same authenticated namespace share a single in-flight LLM generation.
    Followers await the leader's future safely via asyncio.shield.
    """

    def __init__(self) -> None:
        self._in_flight: Dict[FlightKey, asyncio.Future[Any]] = {}
        self._lock = asyncio.Lock()

    async def register_or_wait(self, key: FlightKey) -> Tuple[bool, asyncio.Future[Any]]:
        """
        Atomically checks or registers in-flight work for this flight key.
        The flight key is typically a composite (namespace, fingerprint).
        Returns (is_leader, future).
        """
        async with self._lock:
            if key in self._in_flight:
                return False, self._in_flight[key]

            loop = asyncio.get_running_loop()
            fut: asyncio.Future[Any] = loop.create_future()
            self._in_flight[key] = fut
            return True, fut

    async def complete_success(self, key: FlightKey, result: Any) -> None:
        """Leader marks the shared future as resolved with success."""
        async with self._lock:
            fut = self._in_flight.pop(key, None)
            if fut is not None and not fut.done():
                fut.set_result(result)

    async def complete_failure(self, key: FlightKey, exc: BaseException) -> None:
        """
        Leader marks the shared future resolved with exception or cancellation.
        Marks the exception retrieved immediately to prevent
        'Future exception was never retrieved' warnings when zero followers wait.
        """
        async with self._lock:
            fut = self._in_flight.pop(key, None)
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
