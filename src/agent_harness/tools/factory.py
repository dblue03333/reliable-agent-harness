"""Compose the three fixed mock tools with an isolated incident ledger per bundle."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from agent_harness.tools.data import load_mock_dataset
from agent_harness.tools.incident import IncidentTool
from agent_harness.tools.knowledge_base import KnowledgeBaseTool
from agent_harness.tools.registry import ToolRegistry, ToolSpec
from agent_harness.tools.schemas import (
    CreateIncidentInput,
    CreateIncidentOutput,
    GetServiceStatusInput,
    GetServiceStatusOutput,
    SearchKnowledgeBaseInput,
    SearchKnowledgeBaseOutput,
)
from agent_harness.tools.service_status import ServiceStatusTool


@dataclass(frozen=True)
class MockTools:
    registry: ToolRegistry
    incidents: IncidentTool


def build_mock_tools(directory: Path, *, now: Callable[[], datetime] | None = None) -> MockTools:
    dataset = load_mock_dataset(directory)
    incidents = IncidentTool(now=now)
    registry = ToolRegistry(
        (
            ToolSpec(
                name="search_knowledge_base",
                description="Search synthetic runbooks by keyword; matches do not confirm causes.",
                input_schema=SearchKnowledgeBaseInput,
                output_schema=SearchKnowledgeBaseOutput,
                handler=KnowledgeBaseTool(dataset.knowledge),
            ),
            ToolSpec(
                name="get_service_status",
                description="Read a synthetic service status, not live production telemetry.",
                input_schema=GetServiceStatusInput,
                output_schema=GetServiceStatusOutput,
                handler=ServiceStatusTool(dataset.status, now=now),
            ),
            ToolSpec(
                name="create_incident",
                description="Propose a mock incident; user approval is required to execute.",
                input_schema=CreateIncidentInput,
                output_schema=CreateIncidentOutput,
                handler=incidents,
                side_effect=True,
                requires_approval=True,
            ),
        )
    )
    return MockTools(registry=registry, incidents=incidents)
