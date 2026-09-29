"""Monotonic active-segment accounting and cooperative operation deadlines."""

import asyncio
import time
from collections.abc import Awaitable, Callable

from agent_harness.errors import ErrorCode, HarnessError


class ActiveBudget:
    def __init__(
        self,
        limit_seconds: float,
        used_seconds: float = 0.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._limit = limit_seconds
        self._used = used_seconds
        self._clock = clock
        self._started = clock()

    @property
    def elapsed(self) -> float:
        return self._used + max(0.0, self._clock() - self._started)

    @property
    def remaining(self) -> float:
        return max(0.0, self._limit - self.elapsed)

    def check(self) -> None:
        if self.remaining <= 0:
            raise HarnessError(ErrorCode.RUNTIME_LIMIT, "Active runtime budget exhausted.")

    async def call[T](
        self,
        operation: Callable[[], Awaitable[T]],
        timeout_seconds: float,
        timeout_code: ErrorCode,
    ) -> T:
        self.check()
        remaining = self.remaining
        global_deadline_first = remaining <= timeout_seconds
        timer = asyncio.timeout(min(remaining, timeout_seconds))
        try:
            async with timer:
                result = await operation()
        except TimeoutError:
            if self.remaining <= 0 or (timer.expired() and global_deadline_first):
                raise HarnessError(
                    ErrorCode.RUNTIME_LIMIT, "Active runtime budget exhausted."
                ) from None
            raise HarnessError(timeout_code, "Operation timed out.") from None
        except Exception:
            # An adapter may catch our cancellation and translate it to a domain
            # error. The expired owning deadline still determines classification.
            # External CancelledError is a BaseException and propagates unchanged.
            if timer.expired():
                code = (
                    ErrorCode.RUNTIME_LIMIT
                    if global_deadline_first or self.remaining <= 0
                    else timeout_code
                )
                raise HarnessError(code, "Operation exceeded its deadline.") from None
            raise
        # Providers/handlers must cooperate with cancellation. Detect a swallowed
        # deadline, but a blocking function cannot be forcibly stopped by asyncio.
        if timer.expired():
            code = (
                ErrorCode.RUNTIME_LIMIT
                if global_deadline_first or self.remaining <= 0
                else timeout_code
            )
            raise HarnessError(code, "Operation exceeded its deadline.")
        return result
