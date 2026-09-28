"""Providers propose raw decisions. They receive no store or tool handlers."""

from typing import Annotated, Protocol

from pydantic import Field

from agent_harness.contracts import ContractModel, Identifier, Objective
from agent_harness.models import Message


class LLMRequest(ContractModel):
    execution_id: Identifier
    objective: Objective
    step: Annotated[int, Field(ge=1)]
    messages: tuple[Message, ...] = ()
    repair_instruction: str | None = None


class LLMProvider(Protocol):
    async def generate(self, request: LLMRequest) -> str:
        """Return raw JSON; the harness parses, validates, and controls execution.

        Implementations must cooperate with cancellation. No tool dispatch or
        automatic response-repair loop belongs inside a provider.
        """
        ...
