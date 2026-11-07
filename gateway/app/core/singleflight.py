"""
Cancellation-safe Single-Flight Concurrency Coordinator.
Ensures concurrent identical cache misses share a single upstream generation within the same namespace.
Core modules MUST NOT import FastAPI or Groq SDK.
"""

import asyncio
from typing import Any, Dict, Hashable, Optional, Tuple, Union

from .errors import SingleFlightLeaderCancelledError

FlightKey = Union[Tuple[str, str], str, Hashable]


class SingleFlightCoordinator:
    """
    Coordinates concurrent identical requests so that simultaneous misses within
    the same authenticated namespace share a single in-flight LLM generation.
    Followers await the leader's future safely via asyncio.shield.
    """

    def __init__(self) -> None:
        self._in_flight: Dict[FlightKey, asyncio.Future[Any]] = {}
        self._waiter_counts: Dict[FlightKey, int] = {}
        self._waiter_events: Dict[FlightKey, list[Tuple[int, asyncio.Event]]] = {}

    async def register_or_wait(self, key: FlightKey) -> Tuple[bool, asyncio.Future[Any]]:
        """
        Atomically checks or registers in-flight work for this flight key.
        The flight key is typically a composite (namespace, fingerprint).
        Returns (is_leader, future).
        """
        if key in self._in_flight:
            count = self._waiter_counts.get(key, 0) + 1
            self._waiter_counts[key] = count
            for target_count, event in self._waiter_events.get(key, []):
                if count >= target_count and not event.is_set():
                    event.set()
            return False, self._in_flight[key]

        loop = asyncio.get_running_loop()
        fut: asyncio.Future[Any] = loop.create_future()
        self._in_flight[key] = fut
        self._waiter_counts[key] = 0
        return True, fut

    async def complete_success(
        self,
        key: FlightKey,
        result: Any,
        expected_future: Optional[asyncio.Future[Any]] = None,
        release: bool = True,
    ) -> None:
        """
        Leader marks the shared future as resolved with success.
        Idempotent, synchronous, and immune to cancellation interruptions.
        If release is True (default), removes the key from _in_flight immediately.
        If release is False, resolves waiting followers immediately while retaining the entry
        until release() is explicitly called (e.g. after cache persistence).
        """
        fut = self._in_flight.get(key)
        if release and fut is not None and (expected_future is None or fut is expected_future):
            self._in_flight.pop(key, None)
            self._waiter_counts.pop(key, None)
            self._waiter_events.pop(key, None)
        target = fut if (fut is not None and (expected_future is None or fut is expected_future)) else expected_future
        if target is not None and not target.done():
            target.set_result(result)

    async def release(
        self,
        key: FlightKey,
        expected_future: Optional[asyncio.Future[Any]] = None,
    ) -> None:
        """
        Removes the flight key from _in_flight once persistence or indexing has concluded.
        Immune to cancellation interruptions.
        """
        fut = self._in_flight.get(key)
        if fut is not None and (expected_future is None or fut is expected_future):
            self._in_flight.pop(key, None)
            self._waiter_counts.pop(key, None)
            self._waiter_events.pop(key, None)

    async def complete_failure(
        self,
        key: FlightKey,
        exc: BaseException,
        expected_future: Optional[asyncio.Future[Any]] = None,
    ) -> None:
        """
        Leader marks the shared future resolved with exception or cancellation.
        When leader is cancelled (asyncio.CancelledError), completes future with
        SingleFlightLeaderCancelledError so followers receive a controlled domain error
        rather than being marked cancelled.
        Marks the exception retrieved immediately to prevent
        'Future exception was never retrieved' warnings when zero followers wait.
        Idempotent, synchronous, and immune to cancellation interruptions.
        """
        fut = self._in_flight.get(key)
        if fut is not None and (expected_future is None or fut is expected_future):
            self._in_flight.pop(key, None)
            self._waiter_counts.pop(key, None)
            self._waiter_events.pop(key, None)
        target = fut if (fut is not None and (expected_future is None or fut is expected_future)) else expected_future
        if target is not None and not target.done():
            if isinstance(exc, asyncio.CancelledError):
                target.set_exception(SingleFlightLeaderCancelledError())
            else:
                target.set_exception(exc)
            # Mark exception retrieved to eliminate unretrieved future warning
            try:
                target.exception()
            except (Exception, asyncio.CancelledError):
                pass

    def in_flight_count(self) -> int:
        """Returns the number of currently active in-flight SingleFlight keys."""
        return len(self._in_flight)

    def is_in_flight(self, key: FlightKey) -> bool:
        """Returns True if the given flight key is currently registered in flight."""
        return key in self._in_flight

    @staticmethod
    async def wait_flight(shared_future: asyncio.Future[Any]) -> Any:
        """
        Safely awaits a SingleFlight leader's shared future for a follower task.
        Unlike standard asyncio.shield(), if the follower task is cancelled, this does NOT
        log 'exception in shielded future' to the loop exception handler if the leader
        subsequently completes with an exception or cancellation.
        Guarantees that cancelling the follower task NEVER cancels the shared leader future.
        """
        loop = asyncio.get_running_loop()
        follower_fut: asyncio.Future[Any] = loop.create_future()

        def _on_leader_done(f: asyncio.Future[Any]) -> None:
            if follower_fut.done():
                return
            if f.cancelled():
                follower_fut.cancel()
            elif f.exception() is not None:
                follower_fut.set_exception(f.exception())
                try:
                    follower_fut.exception()
                except (Exception, asyncio.CancelledError):
                    pass
            else:
                follower_fut.set_result(f.result())

        shared_future.add_done_callback(_on_leader_done)
        return await follower_fut

    def waiter_count(self, key: FlightKey) -> int:
        """Returns the number of followers currently waiting for this flight key."""
        return self._waiter_counts.get(key, 0)

    async def wait_for_waiters(
        self,
        key: FlightKey,
        count: int = 1,
        timeout: float = 2.0,
    ) -> None:
        """
        Deterministically awaits until at least `count` followers have registered for `key`.
        Avoids non-deterministic asyncio.sleep() in concurrent tests and production hooks.
        """
        if self._waiter_counts.get(key, 0) >= count:
            return
        event = asyncio.Event()
        self._waiter_events.setdefault(key, []).append((count, event))
        try:
            await asyncio.wait_for(event.wait(), timeout=timeout)
        finally:
            if key in self._waiter_events:
                self._waiter_events[key] = [
                    e for e in self._waiter_events[key] if e[1] is not event
                ]
                if not self._waiter_events[key]:
                    self._waiter_events.pop(key, None)
