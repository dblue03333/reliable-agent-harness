"""Internal call metadata; excluded from every LLM-visible tool input schema."""

from agent_harness.contracts import ContractModel, Identifier


class ToolExecutionContext(ContractModel):
    execution_id: Identifier
    action_id: Identifier

    @property
    def idempotency_key(self) -> str:
        return self.action_id
