"""M0 validates representations, not runtime approval or execution behavior."""

import json
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from agent_harness.errors import DecisionValidationError, ErrorCode, ErrorInfo
from agent_harness.models import (
    ActionOutcome,
    ActionStatus,
    CreateExecutionRequest,
    EventType,
    ExecutionEvent,
    ExecutionState,
    ExecutionStatus,
    FinalDecision,
    IncidentAction,
    TerminationReason,
    ToolCallDecision,
    ToolCallRecord,
    decision_json_schema,
    parse_decision,
)
from agent_harness.tools.schemas import (
    CreateIncidentInput,
    CreateIncidentOutput,
    GetServiceStatusInput,
    GetServiceStatusOutput,
    SearchKnowledgeBaseInput,
    SearchKnowledgeBaseOutput,
)

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def incident_arguments() -> CreateIncidentInput:
    return CreateIncidentInput(
        title="Checkout degraded", description="Checkout health check is degraded.", severity="high"
    )


def successful_action() -> IncidentAction:
    return IncidentAction(
        action_id="act_1",
        execution_id="exec_1",
        arguments=incident_arguments(),
        status=ActionStatus.RESOLVED,
        outcome=ActionOutcome.SUCCEEDED,
        result=CreateIncidentOutput(incident_id="INC-1001", created_at=NOW),
        decided_at=NOW,
        resolved_at=NOW,
    )


def test_decision_discriminator_and_tool_validation_boundary() -> None:
    decision = parse_decision(
        '{"type":"tool_call","tool":"get_service_status",'
        '"arguments":{"service_name":"checkout-api"}}'
    )
    assert isinstance(decision, ToolCallDecision)
    arguments = GetServiceStatusInput.model_validate(decision.arguments)
    assert arguments.service_name == "checkout-api"
    assert isinstance(
        parse_decision('{"type":"final","answer":"Observed degradation."}'), FinalDecision
    )


def test_unknown_tool_is_not_mistaken_for_a_malformed_envelope() -> None:
    # The registry (M1) will report UNKNOWN_TOOL; the parser does not dispatch anything.
    decision = parse_decision('{"type":"tool_call","tool":"unknown","arguments":{}}')
    assert isinstance(decision, ToolCallDecision)
    assert decision.tool == "unknown"


@pytest.mark.parametrize(
    "raw",
    [
        "not JSON",
        '```json\n{"type":"final","answer":"ok"}\n```',
        '{"type":"final","answer":"ok"} trailing prose',
        "[]",
        '{"type":"other"}',
        '{"type":"final","answer":"   "}',
        '{"type":"final","answer":123}',
        '{"type":"final","answer":"ok","approved":true}',
        '{"type":"tool_call","tool":"get_service_status","arguments":[]}',
        '{"type":"tool_call","tool":"get_service_status"}',
        '{"type":"tool_call","tool":"x","arguments":{"value":NaN}}',
        '{"type":"tool_call","tool":"x","arguments":{"value":Infinity}}',
        '{"type":"tool_call","tool":"x","arguments":{"value":1e999}}',
    ],
)
def test_malformed_decisions_raise_safe_typed_errors(raw: str) -> None:
    with pytest.raises(DecisionValidationError) as caught:
        parse_decision(raw)
    assert caught.value.info.code == ErrorCode.INVALID_DECISION
    assert raw not in str(caught.value)


def test_schema_is_generated_from_contracts_and_is_a_fresh_copy() -> None:
    schema = decision_json_schema()
    assert schema["discriminator"]["propertyName"] == "type"
    assert schema["$defs"]["FinalDecision"]["properties"]["answer"]["maxLength"] == 8000
    schema.clear()
    assert "$defs" in decision_json_schema()


@pytest.mark.parametrize("objective", ["", " \n ", "x" * 4001, 42, True])
def test_invalid_objectives_are_rejected(objective: object) -> None:
    with pytest.raises(ValidationError):
        CreateExecutionRequest(objective=objective)


def test_input_limits_and_normalization() -> None:
    assert (
        CreateExecutionRequest(objective="  Investigate checkout  ").objective
        == "Investigate checkout"
    )
    assert len(CreateExecutionRequest(objective="x" * 4000).objective) == 4000
    assert len(SearchKnowledgeBaseInput(query="x" * 500).query) == 500
    with pytest.raises(ValidationError):
        SearchKnowledgeBaseInput(query="x" * 501)
    with pytest.raises(ValidationError):
        GetServiceStatusInput(service_name="x" * 101)
    with pytest.raises(DecisionValidationError):
        parse_decision(json.dumps({"type": "final", "answer": "x" * 8001}))


@pytest.mark.parametrize(
    "changes",
    [
        {"severity": "NUCLEAR"},
        {"title": " "},
        {"title": "x" * 201},
        {"description": "x" * 4001},
        {"severity": 1},
        {"idempotency_key": "attacker-owned"},
        {"approved": True},
    ],
)
def test_incident_contract_rejects_invalid_or_privileged_arguments(changes: dict) -> None:
    raw = {"title": "Checkout", "description": "Observed degradation", "severity": "high"}
    with pytest.raises(ValidationError):
        CreateIncidentInput.model_validate(raw | changes)


def test_incident_snapshot_cannot_be_changed_through_source_or_normal_assignment() -> None:
    raw = {"title": "Checkout", "description": "Observed degradation", "severity": "high"}
    action = IncidentAction(
        execution_id="exec_1", arguments=CreateIncidentInput.model_validate(raw)
    )
    raw["severity"] = "critical"
    assert action.arguments.severity == "high"
    with pytest.raises(ValidationError, match="frozen"):
        action.arguments.severity = "critical"
    exported = action.model_dump(mode="json")
    exported["arguments"]["title"] = "Changed"
    assert action.arguments.title == "Checkout"


@pytest.mark.parametrize("latency", [-1, "12", True, float("nan"), float("inf")])
def test_service_output_rejects_invalid_measurements(latency: object) -> None:
    with pytest.raises(ValidationError):
        GetServiceStatusOutput(
            service_name="checkout-api", status="degraded", latency_ms=latency, checked_at=NOW
        )


def test_json_round_trip_preserves_validated_tool_output() -> None:
    result = GetServiceStatusOutput.model_validate_json(
        '{"service_name":"checkout-api","status":"degraded",'
        '"latency_ms":4200,"checked_at":"2026-01-01T00:00:00Z"}'
    )
    assert result.checked_at.tzinfo is not None
    assert GetServiceStatusOutput.model_validate_json(result.model_dump_json()) == result
    with pytest.raises(ValidationError):
        CreateIncidentOutput(incident_id="INC-1001", created_at=datetime(2026, 1, 1))


def test_kb_empty_results_are_valid_but_excessive_or_invalid_results_are_not() -> None:
    assert SearchKnowledgeBaseOutput.model_validate_json('{"matches":[]}').matches == ()
    match = {
        "document_id": "kb-1",
        "title": "Checkout",
        "excerpt": "Check latency",
        "relevance": 0.5,
    }
    with pytest.raises(ValidationError):
        SearchKnowledgeBaseOutput.model_validate_json(json.dumps({"matches": [match] * 6}))
    for invalid in ({"relevance": 1.1}, {"excerpt": "x" * 1001}, {"relevance": "0.5"}):
        with pytest.raises(ValidationError):
            SearchKnowledgeBaseOutput.model_validate_json(
                json.dumps({"matches": [match | invalid]})
            )


def test_waiting_state_requires_matching_owned_pending_action() -> None:
    action = IncidentAction(execution_id="exec_1", arguments=incident_arguments())
    values = dict(
        execution_id="exec_1",
        objective="Investigate checkout",
        status=ExecutionStatus.WAITING_APPROVAL,
        actions=(action,),
        pending_action_id=action.action_id,
    )
    assert ExecutionState(**values).pending_action_id == action.action_id
    for changes in (
        {"execution_id": "exec_other"},
        {"pending_action_id": "act_other"},
        {"actions": ()},
        {"actions": (action, action)},
        {"status": ExecutionStatus.RUNNING},
    ):
        with pytest.raises(ValidationError):
            ExecutionState(**(values | changes))


@pytest.mark.parametrize(
    "changes",
    [
        {"status": ActionStatus.RESOLVED},
        {"status": ActionStatus.EXECUTING},
        {"status": ActionStatus.REJECTED, "outcome": ActionOutcome.FAILED},
        {"outcome": ActionOutcome.SUCCEEDED},
        {"decided_at": NOW},
    ],
)
def test_inconsistent_action_states_are_rejected(changes: dict) -> None:
    with pytest.raises(ValidationError):
        IncidentAction(execution_id="exec_1", arguments=incident_arguments(), **changes)


def test_unknown_outcome_has_no_trusted_success_result() -> None:
    action = IncidentAction(
        execution_id="exec_1",
        arguments=incident_arguments(),
        status=ActionStatus.RESOLVED,
        outcome=ActionOutcome.OUTCOME_UNKNOWN,
        error=ErrorInfo(code=ErrorCode.OUTCOME_UNKNOWN, message="Creation could not be confirmed."),
        decided_at=NOW,
        resolved_at=NOW,
    )
    assert action.result is None
    with pytest.raises(ValidationError):
        IncidentAction.model_validate(
            action.model_dump()
            | {"result": CreateIncidentOutput(incident_id="INC-1", created_at=NOW)}
        )


@pytest.mark.parametrize(
    ("status", "reason", "code"),
    [
        (ExecutionStatus.FAILED, TerminationReason.ERROR, ErrorCode.LLM_TIMEOUT),
        (
            ExecutionStatus.LIMIT_EXCEEDED,
            TerminationReason.LLM_STEP_BUDGET_EXHAUSTED,
            ErrorCode.STEP_LIMIT,
        ),
    ],
)
def test_terminal_execution_preserves_successful_incident(
    status: ExecutionStatus, reason: TerminationReason, code: ErrorCode
) -> None:
    state = ExecutionState(
        execution_id="exec_1",
        objective="Investigate checkout",
        status=status,
        termination_reason=reason,
        actions=(successful_action(),),
        error=ErrorInfo(code=code, message="Final generation could not continue."),
    )
    restored = ExecutionState.model_validate_json(state.model_dump_json())
    assert restored.actions[0].result.incident_id == "INC-1001"
    assert restored.final_answer is None
    assert restored.status == status


@pytest.mark.parametrize(
    "changes",
    [
        {"status": ExecutionStatus.COMPLETED},
        {"final_answer": "Pretend complete"},
        {"status": ExecutionStatus.FAILED},
        {"status": ExecutionStatus.LIMIT_EXCEEDED, "termination_reason": TerminationReason.ERROR},
        {"step_count": -1},
        {"step_count": True},
        {"repair_attempt_count": 1, "step_count": 0},
        {"active_runtime_seconds": float("nan")},
        {"termination_reason": TerminationReason.FINAL_ANSWER},
    ],
)
def test_invalid_execution_snapshots_are_rejected(changes: dict) -> None:
    with pytest.raises(ValidationError):
        ExecutionState(objective="Investigate checkout", **changes)


def test_completed_snapshot_and_unique_execution_ids() -> None:
    state = ExecutionState(
        objective="Investigate checkout",
        status=ExecutionStatus.COMPLETED,
        final_answer="Checkout is degraded.",
        termination_reason=TerminationReason.FINAL_ANSWER,
    )
    assert ExecutionState.model_validate_json(state.model_dump_json()) == state
    assert ExecutionState(objective="A").execution_id != ExecutionState(objective="B").execution_id


def test_attempt_and_event_require_valid_audit_fields() -> None:
    with pytest.raises(ValidationError):
        ToolCallRecord(
            call_id="call_1",
            tool="get_service_status",
            step=1,
            attempt=1,
            outcome=ActionOutcome.SUCCEEDED,
            duration_ms=0.1,
        )
    for changes in ({"sequence": 0}, {"duration_ms": -1}, {"attempt": 0}):
        with pytest.raises(ValidationError):
            ExecutionEvent.model_validate(
                dict(execution_id="exec_1", sequence=1, event_type=EventType.LLM_REQUESTED)
                | changes
            )
