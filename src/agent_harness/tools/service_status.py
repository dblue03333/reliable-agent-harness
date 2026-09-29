"""Synthetic status lookup. checked_at is lookup time, not a real health probe."""

from collections.abc import Callable
from datetime import UTC, datetime

from agent_harness.errors import PermanentToolError
from agent_harness.tools.context import ToolExecutionContext
from agent_harness.tools.data import ServiceDataset
from agent_harness.tools.schemas import GetServiceStatusInput, GetServiceStatusOutput


class ServiceStatusTool:
    def __init__(
        self, dataset: ServiceDataset, *, now: Callable[[], datetime] | None = None
    ) -> None:
        self._services = {service.service_name: service for service in dataset.services}
        self._now = now if now is not None else lambda: datetime.now(UTC)

    async def __call__(
        self, arguments: GetServiceStatusInput, context: ToolExecutionContext | None = None
    ) -> GetServiceStatusOutput:
        service = self._services.get(arguments.service_name)
        if service is None:
            raise PermanentToolError()
        return GetServiceStatusOutput(
            service_name=service.service_name,
            status=service.status,
            latency_ms=service.latency_ms,
            checked_at=self._now(),
        )
