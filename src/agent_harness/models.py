"""Decision and state contracts. Locks, transitions, dispatch, and budgets come later."""

import json
import math
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Literal, Self
from uuid import uuid4

from pydantic import AwareDatetime, Field, JsonValue, TypeAdapter, model_validator

from agent_harness.contracts import ContractModel, FinalAnswer, Identifier, Objective
from agent_harness.errors import DecisionValidationError, ErrorInfo
from agent_harness.tools.schemas import CreateIncidentInput, CreateIncidentOutput


def utc_now() -> datetime:
    return datetime.now(UTC)


class ExecutionStatus(StrEnum):
    CREATED = "created"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    COMPLETED = "completed"
    FAILED = "failed"
    LIMIT_EXCEEDED = "limit_exceeded"


class TerminationReason(StrEnum):
    FINAL_ANSWER = "final_answer"
    ERROR = "error"
    CANCELLED = "cancelled"
    OUTCOME_UNKNOWN = "outcome_unknown"
    LLM_STEP_BUDGET_EXHAUSTED = "llm_step_budget_exhausted"
    ACTIVE_RUNTIME_EXHAUSTED = "active_runtime_exhausted"


class ActionStatus(StrEnum):
    PENDING = "pending"
    EXECUTING = "executing"
    RESOLVED = "resolved"
    REJECTED = "rejected"


class ActionOutcome(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    OUTCOME_UNKNOWN = "outcome_unknown"


class ToolCallDecision(ContractModel):
    type: Literal["tool_call"]
    tool: Identifier
    arguments: dict[str, JsonValue]


class FinalDecision(ContractModel):
    type: Literal["final"]
    answer: FinalAnswer


AgentDecision = Annotated[ToolCallDecision | FinalDecision, Field(discriminator="type")]
_decision_adapter = TypeAdapter(AgentDecision)


def _finite_json_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("Decision JSON requires finite numbers.")
    return number


def parse_decision(raw_json: str) -> AgentDecision:
    """Parse one complete JSON decision, without prose extraction or tool execution."""
    try:
        # JSON parsers may accept NaN/Infinity or overflow 1e999 to infinity.
        data = json.loads(
            raw_json, parse_constant=_finite_json_float, parse_float=_finite_json_float
        )
        return _decision_adapter.validate_python(data)
    except (ValueError, TypeError):
        # Do not put potentially sensitive LLM output into public exception messages.
        raise DecisionValidationError() from None


def decision_json_schema() -> dict[str, JsonValue]:
    """Return a fresh contract-derived schema; provider compatibility is checked in M3."""
    return _decision_adapter.json_schema()


class CreateExecutionRequest(ContractModel):
    objective: Objective


class Message(ContractModel):
    role: Literal["system", "user", "assistant", "tool"]
    content: Annotated[str, Field(min_length=1, max_length=16000)]


class IncidentAction(ContractModel):
    """Only incident creation requires approval in V1. This is not an authorization check."""

    action_id: Identifier = Field(default_factory=lambda: f"act_{uuid4().hex}")
    execution_id: Identifier
    tool: Literal["create_incident"] = "create_incident"
    arguments: CreateIncidentInput
    status: ActionStatus = ActionStatus.PENDING
    outcome: ActionOutcome | None = None
    result: CreateIncidentOutput | None = None
    error: ErrorInfo | None = None
    created_at: AwareDatetime = Field(default_factory=utc_now)
    decided_at: AwareDatetime | None = None
    resolved_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def validate_outcome(self) -> Self:
        if self.status != ActionStatus.RESOLVED:
            if any(value is not None for value in (self.outcome, self.result, self.error)):
                raise ValueError("Only resolved actions have an outcome, result, or error.")
            if self.resolved_at is not None:
                raise ValueError("Only resolved actions have a resolution timestamp.")
        elif self.outcome is None or self.resolved_at is None:
            raise ValueError("Resolved actions require an outcome and resolution timestamp.")
        elif self.outcome == ActionOutcome.SUCCEEDED:
            if self.result is None or self.error is not None:
                raise ValueError("Successful actions require a result and no error.")
            if self.decided_at is None:
                raise ValueError("Successful incident actions require a decision timestamp.")
        elif self.error is None or self.result is not None:
            raise ValueError("Failed or unknown actions require an error and no trusted result.")

        if self.status in (ActionStatus.EXECUTING, ActionStatus.REJECTED):
            if self.decided_at is None:
                raise ValueError("Claimed actions require a decision timestamp.")
        if self.status == ActionStatus.PENDING and self.decided_at is not None:
            raise ValueError("Pending actions cannot have a decision timestamp.")
        return self


class ToolCallRecord(ContractModel):
    """One attempt; result data is trusted only after the registry validates it in M1."""

    call_id: Identifier
    tool: Identifier
    action_id: Identifier | None = None
    step: Annotated[int, Field(ge=1)]
    attempt: Annotated[int, Field(ge=1)]
    outcome: ActionOutcome
    duration_ms: Annotated[float, Field(ge=0)]
    result: dict[str, JsonValue] | None = None
    error: ErrorInfo | None = None

    @model_validator(mode="after")
    def validate_result(self) -> Self:
        if self.outcome == ActionOutcome.SUCCEEDED:
            if self.result is None or self.error is not None:
                raise ValueError("Successful attempts require a result and no error.")
        elif self.error is None or self.result is not None:
            raise ValueError("Failed or unknown attempts require an error and no trusted result.")
        return self


class ExecutionState(ContractModel):
    """A validated snapshot, not a mutable store or a transition engine."""

    execution_id: Identifier = Field(default_factory=lambda: f"exec_{uuid4().hex}")
    objective: Objective
    status: ExecutionStatus = ExecutionStatus.CREATED
    termination_reason: TerminationReason | None = None
    step_count: Annotated[int, Field(ge=0)] = 0
    repair_attempt_count: Annotated[int, Field(ge=0, le=1)] = 0
    active_runtime_seconds: Annotated[float, Field(ge=0)] = 0.0
    messages: tuple[Message, ...] = ()
    actions: tuple[IncidentAction, ...] = ()
    tool_history: tuple[ToolCallRecord, ...] = ()
    pending_action_id: Identifier | None = None
    final_answer: FinalAnswer | None = None
    error: ErrorInfo | None = None
    created_at: AwareDatetime = Field(default_factory=utc_now)
    updated_at: AwareDatetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def validate_snapshot(self) -> Self:
        action_ids = [action.action_id for action in self.actions]
        if len(set(action_ids)) != len(action_ids):
            raise ValueError("Action IDs must be unique within an execution.")
        if any(action.execution_id != self.execution_id for action in self.actions):
            raise ValueError("Actions must belong to this execution.")

        pending = [action for action in self.actions if action.status == ActionStatus.PENDING]
        executing = [action for action in self.actions if action.status == ActionStatus.EXECUTING]
        if self.status == ExecutionStatus.WAITING_APPROVAL:
            if len(pending) != 1 or pending[0].action_id != self.pending_action_id or executing:
                raise ValueError("Waiting execution requires exactly one matching pending action.")
        elif pending or self.pending_action_id is not None:
            raise ValueError("Only waiting executions may retain pending actions.")
        if executing and (self.status != ExecutionStatus.RUNNING or len(executing) != 1):
            raise ValueError("Only running executions may have one executing action.")

        if self.status == ExecutionStatus.COMPLETED:
            if self.termination_reason != TerminationReason.FINAL_ANSWER or not self.final_answer:
                raise ValueError("Completed executions require a final answer and its reason.")
            if self.error is not None:
                raise ValueError("Completed executions cannot have an execution error.")
        elif self.final_answer is not None:
            raise ValueError("Only completed executions have a final answer.")

        if self.status == ExecutionStatus.LIMIT_EXCEEDED:
            if self.termination_reason not in (
                TerminationReason.LLM_STEP_BUDGET_EXHAUSTED,
                TerminationReason.ACTIVE_RUNTIME_EXHAUSTED,
            ):
                raise ValueError("Limit termination requires a budget reason.")
        elif self.status == ExecutionStatus.FAILED:
            if self.termination_reason not in (
                TerminationReason.ERROR,
                TerminationReason.CANCELLED,
                TerminationReason.OUTCOME_UNKNOWN,
            ):
                raise ValueError("Failed executions require an error termination reason.")
        elif self.status != ExecutionStatus.COMPLETED:
            if self.termination_reason is not None or self.error is not None:
                raise ValueError("Active executions cannot have terminal error metadata.")
        if self.status in (ExecutionStatus.FAILED, ExecutionStatus.LIMIT_EXCEEDED):
            if self.error is None:
                raise ValueError("Failed or limited executions require an error.")
        if self.repair_attempt_count > self.step_count:
            raise ValueError("Repair requests count as LLM steps.")
        return self


class EventType(StrEnum):
    EXECUTION_CREATED = "execution_created"
    STATE_CHANGED = "state_changed"
    LLM_REQUESTED = "llm_requested"
    LLM_RETURNED = "llm_returned"
    LLM_INVALID = "llm_invalid"
    ACTION_PROPOSED = "action_proposed"
    ACTION_CLAIMED = "action_claimed"
    ACTION_REJECTED = "action_rejected"
    TOOL_STARTED = "tool_started"
    TOOL_FINISHED = "tool_finished"
    RETRY_SCHEDULED = "retry_scheduled"
    BUDGET_EXHAUSTED = "budget_exhausted"
    EXECUTION_TERMINATED = "execution_terminated"


class ExecutionEvent(ContractModel):
    execution_id: Identifier
    sequence: Annotated[int, Field(ge=1)]
    timestamp: AwareDatetime = Field(default_factory=utc_now)
    event_type: EventType
    step: Annotated[int, Field(ge=0)] = 0
    action_id: Identifier | None = None
    tool_call_id: Identifier | None = None
    tool: Identifier | None = None
    attempt: Annotated[int, Field(ge=1)] | None = None
    duration_ms: Annotated[float, Field(ge=0)] | None = None
    outcome: ActionOutcome | None = None
    error: ErrorInfo | None = None
