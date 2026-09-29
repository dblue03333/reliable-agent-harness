"""Load explicit local synthetic fixtures once, before serving tool calls."""

from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import Field, StringConstraints, ValidationError, model_validator

from agent_harness.contracts import ContractModel, Identifier
from agent_harness.errors import MockDataError


class Runbook(ContractModel):
    document_id: Identifier
    title: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
    content: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4000)]
    services: Annotated[tuple[Identifier, ...], Field(min_length=1)]


class ServiceObservation(ContractModel):
    service_name: Identifier
    status: Literal["healthy", "degraded", "down"]
    latency_ms: Annotated[float, Field(ge=0)]


class KnowledgeDataset(ContractModel):
    runbooks: Annotated[tuple[Runbook, ...], Field(min_length=1)]

    @model_validator(mode="after")
    def unique_ids(self) -> Self:
        ids = [book.document_id for book in self.runbooks]
        if len(ids) != len(set(ids)):
            raise ValueError("Runbook IDs must be unique.")
        return self


class ServiceDataset(ContractModel):
    services: Annotated[tuple[ServiceObservation, ...], Field(min_length=1)]

    @model_validator(mode="after")
    def unique_names(self) -> Self:
        names = [service.service_name for service in self.services]
        if len(names) != len(set(names)):
            raise ValueError("Service names must be unique.")
        return self


class MockDataset(ContractModel):
    knowledge: KnowledgeDataset
    status: ServiceDataset

    @model_validator(mode="after")
    def known_services(self) -> Self:
        names = {service.service_name for service in self.status.services}
        if any(set(book.services) - names for book in self.knowledge.runbooks):
            raise ValueError("Runbooks must reference known mock services.")
        return self


def load_mock_dataset(directory: Path) -> MockDataset:
    """Path is application configuration, never a tool argument supplied by the LLM."""
    try:
        return MockDataset(
            knowledge=KnowledgeDataset.model_validate_json(
                (directory / "knowledge_base.json").read_text(encoding="utf-8")
            ),
            status=ServiceDataset.model_validate_json(
                (directory / "service_status.json").read_text(encoding="utf-8")
            ),
        )
    except (OSError, UnicodeError, ValidationError):
        raise MockDataError() from None
