"""Process-local mock side effect. Approval is the caller's responsibility."""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from agent_harness.errors import ApprovalRequiredError, IncidentConflictError
from agent_harness.tools.context import ToolExecutionContext
from agent_harness.tools.schemas import CreateIncidentInput, CreateIncidentOutput


@dataclass(frozen=True)
class _LedgerEntry:
    execution_id: str
    arguments: CreateIncidentInput
    result: CreateIncidentOutput


class IncidentTool:
    def __init__(self, *, now: Callable[[], datetime] | None = None) -> None:
        self._now = now if now is not None else lambda: datetime.now(UTC)
        self._lock = asyncio.Lock()
        self._ledger: dict[str, _LedgerEntry] = {}

    @property
    def incident_count(self) -> int:
        return len(self._ledger)

    async def __call__(
        self, arguments: CreateIncidentInput, context: ToolExecutionContext | None = None
    ) -> CreateIncidentOutput:
        if context is None:
            raise ApprovalRequiredError()
        async with self._lock:
            existing = self._ledger.get(context.idempotency_key)
            if existing is not None:
                if existing.execution_id != context.execution_id or existing.arguments != arguments:
                    raise IncidentConflictError()
                return existing.result

            # No await between lookup and commit. Store before returning a response,
            # so a wrapper losing the response can replay the same key safely.
            result = CreateIncidentOutput(
                incident_id=f"INC-{1001 + len(self._ledger)}", created_at=self._now()
            )
            self._ledger[context.idempotency_key] = _LedgerEntry(
                execution_id=context.execution_id, arguments=arguments, result=result
            )
            return result
