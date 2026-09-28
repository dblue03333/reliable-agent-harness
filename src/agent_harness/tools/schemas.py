"""The three LLM-visible tool contracts; execution metadata is deliberately absent."""

from typing import Annotated, Literal

from pydantic import AwareDatetime, Field, StringConstraints

from agent_harness.contracts import ContractModel, Identifier


class SearchKnowledgeBaseInput(ContractModel):
    query: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]


class KnowledgeMatch(ContractModel):
    document_id: Identifier
    title: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
    excerpt: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1000)]
    relevance: Annotated[float, Field(ge=0, le=1)]


class SearchKnowledgeBaseOutput(ContractModel):
    matches: Annotated[tuple[KnowledgeMatch, ...], Field(max_length=5)] = ()


class GetServiceStatusInput(ContractModel):
    service_name: Identifier


class GetServiceStatusOutput(ContractModel):
    service_name: Identifier
    status: Literal["healthy", "degraded", "down"]
    latency_ms: Annotated[float, Field(ge=0)]
    checked_at: AwareDatetime


class CreateIncidentInput(ContractModel):
    """Scalar-only frozen model: safe to retain as an exact approval snapshot."""

    title: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
    description: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4000)
    ]
    severity: Literal["low", "medium", "high", "critical"]


class CreateIncidentOutput(ContractModel):
    incident_id: Identifier
    status: Literal["created"] = "created"
    created_at: AwareDatetime
