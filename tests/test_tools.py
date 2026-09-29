"""M1 tests exercise real mock handlers, the registry, and process-local deduplication."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from agent_harness.errors import (
    ApprovalRequiredError,
    ErrorCode,
    IncidentConflictError,
    MockDataError,
    PermanentToolError,
    ToolHandlerError,
    ToolInputValidationError,
    ToolOutputValidationError,
    TransientToolError,
    UnknownToolError,
)
from agent_harness.models import ActionStatus, IncidentAction
from agent_harness.tools.context import ToolExecutionContext
from agent_harness.tools.data import KnowledgeDataset, Runbook, load_mock_dataset
from agent_harness.tools.demo import run as run_demo
from agent_harness.tools.factory import build_mock_tools
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

DATA_DIR = Path(__file__).resolve().parents[1] / "mock_data"
NOW = datetime(2026, 1, 1, tzinfo=UTC)
ARGS = {"title": "Checkout degradation", "description": "Observed latency.", "severity": "high"}


def claimed_action(**changes) -> IncidentAction:
    """Trusted caller fixture, not evidence of actual human approval (M4)."""
    return IncidentAction(
        **(
            dict(
                execution_id="exec_1",
                action_id="act_1",
                arguments=CreateIncidentInput(**ARGS),
                status=ActionStatus.EXECUTING,
                decided_at=NOW,
            )
            | changes
        )
    )


def registry_for_status(handler) -> ToolRegistry:
    return ToolRegistry(
        [
            ToolSpec(
                name="get_service_status",
                description="test fixture",
                input_schema=GetServiceStatusInput,
                output_schema=GetServiceStatusOutput,
                handler=handler,
            )
        ]
    )


def registry_for_incident(handler) -> ToolRegistry:
    return ToolRegistry(
        [
            ToolSpec(
                name="create_incident",
                description="test fixture",
                input_schema=CreateIncidentInput,
                output_schema=CreateIncidentOutput,
                handler=handler,
                requires_approval=True,
                side_effect=True,
            )
        ]
    )


def test_dataset_has_required_services_runbooks_and_known_references() -> None:
    dataset = load_mock_dataset(DATA_DIR)
    assert len(dataset.status.services) == 5
    assert len(dataset.knowledge.runbooks) == 8
    assert {item.status for item in dataset.status.services} == {"healthy", "degraded", "down"}
    names = {item.service_name for item in dataset.status.services}
    assert all(set(book.services) <= names for book in dataset.knowledge.runbooks)


@pytest.mark.parametrize(
    "invalid",
    [
        "missing",
        "json",
        "duplicate-service",
        "duplicate-book",
        "unknown-reference",
        "bad-latency",
        "extra-field",
    ],
)
def test_dataset_load_fails_closed_without_exposing_file_contents(tmp_path, invalid: str) -> None:
    knowledge = json.loads((DATA_DIR / "knowledge_base.json").read_text())
    status = json.loads((DATA_DIR / "service_status.json").read_text())
    if invalid == "duplicate-service":
        status["services"].append(status["services"][0])
    elif invalid == "duplicate-book":
        knowledge["runbooks"].append(knowledge["runbooks"][0])
    elif invalid == "unknown-reference":
        knowledge["runbooks"][0]["services"] = ["not-a-service"]
    elif invalid == "bad-latency":
        status["services"][0]["latency_ms"] = "4200"
    elif invalid == "extra-field":
        status["unrecognized"] = "test-only-private-value"
    (tmp_path / "service_status.json").write_text(json.dumps(status))
    if invalid != "missing":
        (tmp_path / "knowledge_base.json").write_text(
            "test-only-private-value" if invalid == "json" else json.dumps(knowledge)
        )
    with pytest.raises(MockDataError) as caught:
        load_mock_dataset(tmp_path)
    assert caught.value.info.code == ErrorCode.INVALID_MOCK_DATA
    assert "test-only-private-value" not in str(caught.value)


async def test_status_lookup_uses_injected_clock_and_known_service() -> None:
    tools = build_mock_tools(DATA_DIR, now=lambda: NOW)
    result = await tools.registry.execute("get_service_status", {"service_name": "checkout-api"})
    assert isinstance(result, GetServiceStatusOutput)
    assert result.status == "degraded"
    assert result.latency_ms == 4200
    assert result.checked_at == NOW
    with pytest.raises(PermanentToolError):
        await tools.registry.execute("get_service_status", {"service_name": "missing"})


async def test_search_matches_are_deterministic_bounded_and_case_insensitive() -> None:
    registry = build_mock_tools(DATA_DIR).registry
    first = await registry.execute("search_knowledge_base", {"query": "checkout timeout"})
    second = await registry.execute("search_knowledge_base", {"query": "CHECKOUT timeout checkout"})
    assert isinstance(first, SearchKnowledgeBaseOutput)
    assert first == second
    assert first.matches[0].document_id == "kb-001"
    assert all(0 < match.relevance <= 1 and len(match.excerpt) <= 1000 for match in first.matches)
    assert len(first.matches) <= 5
    assert first.matches == tuple(
        sorted(first.matches, key=lambda m: (-m.relevance, m.document_id))
    )
    for query in ("zxqnonexistent", "!!!"):
        empty = await registry.execute("search_knowledge_base", {"query": query})
        assert empty.matches == ()


async def test_search_caps_results_and_breaks_ties_independent_of_input_order() -> None:
    books = tuple(
        Runbook(document_id=f"kb-{i}", title="timeout", content="x" * 1500, services=("api",))
        for i in reversed(range(8))
    )
    tool = KnowledgeBaseTool(KnowledgeDataset(runbooks=books))
    result = await tool(SearchKnowledgeBaseInput(query="timeout missing"))
    assert [match.document_id for match in result.matches] == [f"kb-{i}" for i in range(5)]
    assert all(match.relevance == 0.5 and len(match.excerpt) == 1000 for match in result.matches)


async def test_unknown_tool_and_invalid_arguments_never_reach_handler() -> None:
    calls = []

    async def handler(arguments, context):
        calls.append(arguments)
        pytest.fail("Invalid tool calls must never dispatch.")

    registry = registry_for_status(handler)
    with pytest.raises(UnknownToolError):
        await registry.execute("unregistered", {})
    for arguments in (
        {},
        {"service_name": 9},
        {"service_name": " "},
        {"service_name": "checkout-api", "approved": True},
        [],
        "{}",
    ):
        with pytest.raises(ToolInputValidationError):
            await registry.execute("get_service_status", arguments)
    assert calls == []


async def test_prepare_validates_snapshot_without_execution() -> None:
    tools = build_mock_tools(DATA_DIR)
    raw = dict(ARGS)
    call = tools.registry.prepare("create_incident", raw)
    raw["severity"] = "critical"
    assert call.arguments.severity == "high"
    assert call.requires_approval and call.side_effect
    assert tools.incidents.incident_count == 0
    with pytest.raises(ValidationError):
        call.arguments.severity = "critical"


async def test_normal_execution_cannot_create_incident_or_supply_internal_context() -> None:
    tools = build_mock_tools(DATA_DIR)
    with pytest.raises(ApprovalRequiredError):
        await tools.registry.execute("create_incident", ARGS)
    for forged in ({"approved": True}, {"idempotency_key": "act_1"}, {"execution_id": "exec_1"}):
        with pytest.raises(ToolInputValidationError):
            await tools.registry.execute("create_incident", ARGS | forged)
    with pytest.raises(ApprovalRequiredError):
        await tools.incidents(CreateIncidentInput(**ARGS))
    pending = IncidentAction(execution_id="exec_1", arguments=CreateIncidentInput(**ARGS))
    with pytest.raises(ApprovalRequiredError):
        await tools.registry.execute_claimed_incident(pending)
    with pytest.raises(ApprovalRequiredError):
        await tools.registry.execute_claimed_incident(claimed_action(status=ActionStatus.REJECTED))
    assert tools.incidents.incident_count == 0


def test_registry_definitions_hide_internal_context_and_do_not_leak_mutable_metadata() -> None:
    registry = build_mock_tools(DATA_DIR).registry
    definitions = registry.descriptions()
    assert {item["name"] for item in definitions} == {
        "search_knowledge_base",
        "get_service_status",
        "create_incident",
    }
    incident = next(item for item in definitions if item["name"] == "create_incident")
    assert set(incident["parameters"]["properties"]) == {"title", "description", "severity"}
    incident["parameters"]["properties"].clear()
    fresh = next(item for item in registry.descriptions() if item["name"] == "create_incident")
    assert "severity" in fresh["parameters"]["properties"]


def test_registration_rejects_duplicate_names_and_unsafe_side_effect_policy() -> None:
    async def handler(arguments, context):
        return {}

    kwargs = dict(
        description="test",
        input_schema=CreateIncidentInput,
        output_schema=CreateIncidentOutput,
        handler=handler,
    )
    for changes in ({"side_effect": True}, {}, {"requires_approval": True}):
        with pytest.raises(ValueError):
            ToolSpec(name="create_incident", **kwargs, **changes)
    with pytest.raises(ValueError):
        ToolSpec(name="some_write_tool", side_effect=True, **kwargs)
    spec = ToolSpec(name="create_incident", side_effect=True, requires_approval=True, **kwargs)
    with pytest.raises(ValueError):
        ToolRegistry((spec, spec))


@pytest.mark.parametrize(
    "result",
    [
        {"status": "healthy"},
        {"service_name": "checkout-api", "status": "unknown", "latency_ms": 10, "checked_at": NOW},
        {
            "service_name": "checkout-api",
            "status": "healthy",
            "latency_ms": "10",
            "checked_at": NOW,
        },
        {
            "service_name": "checkout-api",
            "status": "healthy",
            "latency_ms": float("nan"),
            "checked_at": NOW,
        },
        "not-a-dict",
        GetServiceStatusOutput.model_construct(
            service_name="checkout-api", status="healthy", latency_ms=-1, checked_at=NOW
        ),
    ],
)
async def test_registry_revalidates_handler_outputs_including_constructed_models(result) -> None:
    calls = 0

    async def handler(arguments, context):
        nonlocal calls
        calls += 1
        return result

    with pytest.raises(ToolOutputValidationError):
        await registry_for_status(handler).execute(
            "get_service_status", {"service_name": "checkout-api"}
        )
    assert calls == 1


@pytest.mark.parametrize("outer_model", [True, False])
async def test_malformed_nested_models_are_not_trusted_or_warned_into_logs(
    recwarn, outer_model: bool
) -> None:
    from agent_harness.tools.schemas import KnowledgeMatch

    bad_match = KnowledgeMatch.model_construct(
        document_id="kb-1", title="test", excerpt="test", relevance="sensitive-test-value"
    )

    async def handler(arguments, context):
        if outer_model:
            return SearchKnowledgeBaseOutput.model_construct(matches=(bad_match,))
        return {"matches": (bad_match,)}

    registry = ToolRegistry(
        [
            ToolSpec(
                name="search_knowledge_base",
                description="test",
                input_schema=SearchKnowledgeBaseInput,
                output_schema=SearchKnowledgeBaseOutput,
                handler=handler,
            )
        ]
    )
    with pytest.raises(ToolOutputValidationError) as caught:
        await registry.execute("search_knowledge_base", {"query": "test"})
    assert "sensitive-test-value" not in str(caught.value)
    assert not recwarn


async def test_validation_error_raised_inside_handler_is_output_failure() -> None:
    async def handler(arguments, context):
        return GetServiceStatusOutput(
            service_name="checkout-api", status="healthy", latency_ms=-1, checked_at=NOW
        )

    with pytest.raises(ToolOutputValidationError):
        await registry_for_status(handler).execute(
            "get_service_status", {"service_name": "checkout-api"}
        )


async def test_transient_error_is_preserved_without_retrying() -> None:
    calls = 0

    async def handler(arguments, context):
        nonlocal calls
        calls += 1
        raise TransientToolError()

    with pytest.raises(TransientToolError):
        await registry_for_status(handler).execute(
            "get_service_status", {"service_name": "checkout-api"}
        )
    assert calls == 1


async def test_unexpected_handler_error_has_safe_public_message() -> None:
    async def handler(arguments, context):
        raise RuntimeError("test-only-sensitive-detail")

    with pytest.raises(ToolHandlerError) as caught:
        await registry_for_status(handler).execute(
            "get_service_status", {"service_name": "checkout-api"}
        )
    assert "test-only-sensitive-detail" not in str(caught.value)


async def test_cancellation_propagates_through_registry() -> None:
    started = asyncio.Event()

    async def handler(arguments, context):
        started.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(
        registry_for_status(handler).execute("get_service_status", {"service_name": "checkout-api"})
    )
    await asyncio.wait_for(started.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_incident_same_key_returns_original_id_and_timestamp() -> None:
    times = iter((NOW, NOW + timedelta(seconds=1)))
    tools = build_mock_tools(DATA_DIR, now=lambda: next(times))
    first = await tools.registry.execute_claimed_incident(claimed_action())
    second = await tools.registry.execute_claimed_incident(claimed_action())
    assert first == second
    assert first.incident_id == "INC-1001"
    assert first.created_at == NOW
    assert tools.incidents.incident_count == 1
    distinct = await tools.registry.execute_claimed_incident(claimed_action(action_id="act_2"))
    assert distinct.incident_id == "INC-1002"
    assert distinct.created_at == NOW + timedelta(seconds=1)


async def test_same_key_changed_payload_or_execution_conflicts() -> None:
    tools = build_mock_tools(DATA_DIR, now=lambda: NOW)
    first = await tools.registry.execute_claimed_incident(claimed_action())
    for changes in (
        {"arguments": CreateIncidentInput(**(ARGS | {"severity": "critical"}))},
        {"execution_id": "exec_other"},
    ):
        with pytest.raises(IncidentConflictError):
            await tools.registry.execute_claimed_incident(claimed_action(**changes))
    assert await tools.registry.execute_claimed_incident(claimed_action()) == first
    assert tools.incidents.incident_count == 1


async def test_registry_revalidates_claimed_snapshot_before_dispatch() -> None:
    tools = build_mock_tools(DATA_DIR)
    invalid = claimed_action().model_copy(
        update={
            "arguments": CreateIncidentInput.model_construct(**(ARGS | {"severity": "NUCLEAR"}))
        }
    )
    with pytest.raises(ApprovalRequiredError):
        await tools.registry.execute_claimed_incident(invalid)
    assert tools.incidents.incident_count == 0


async def test_concurrent_same_key_calls_share_one_incident() -> None:
    tool = IncidentTool(now=lambda: NOW)
    started = [asyncio.Event() for _ in range(2)]

    async def caller(index):
        started[index].set()
        return await tool(
            CreateIncidentInput(**ARGS),
            ToolExecutionContext(execution_id="exec_1", action_id="act_1"),
        )

    # Hold the ledger lock so both callers demonstrably contend before either commits.
    async with tool._lock:
        tasks = [asyncio.create_task(caller(index)) for index in range(2)]
        await asyncio.wait_for(asyncio.gather(*(event.wait() for event in started)), timeout=1)
        assert all(not task.done() for task in tasks)
    results = await asyncio.wait_for(asyncio.gather(*tasks), timeout=1)
    assert results[0] == results[1]
    assert tool.incident_count == 1


async def test_concurrent_distinct_keys_get_distinct_incidents() -> None:
    tools = build_mock_tools(DATA_DIR, now=lambda: NOW)
    results = await asyncio.gather(
        *(
            tools.registry.execute_claimed_incident(claimed_action(action_id=f"act_{i}"))
            for i in range(10)
        )
    )
    assert len({result.incident_id for result in results}) == 10
    assert tools.incidents.incident_count == 10


async def test_concurrent_changed_payload_with_same_key_has_one_winner() -> None:
    tool = IncidentTool(now=lambda: NOW)
    context = ToolExecutionContext(execution_id="exec_1", action_id="act_1")
    started = [asyncio.Event(), asyncio.Event()]

    async def caller(index):
        started[index].set()
        arguments = CreateIncidentInput(**(ARGS | {"severity": ("high", "critical")[index]}))
        return await tool(arguments, context)

    async with tool._lock:
        tasks = [asyncio.create_task(caller(index)) for index in range(2)]
        await asyncio.wait_for(asyncio.gather(*(event.wait() for event in started)), timeout=1)
    results = await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=1)
    assert sum(isinstance(result, CreateIncidentOutput) for result in results) == 1
    assert sum(isinstance(result, IncidentConflictError) for result in results) == 1
    assert tool.incident_count == 1


async def test_cancellation_while_waiting_for_ledger_lock_creates_nothing() -> None:
    tool = IncidentTool(now=lambda: NOW)
    started = asyncio.Event()

    async def caller():
        started.set()
        return await tool(
            CreateIncidentInput(**ARGS),
            ToolExecutionContext(execution_id="exec_1", action_id="act_1"),
        )

    async with tool._lock:
        task = asyncio.create_task(caller())
        await asyncio.wait_for(started.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert tool.incident_count == 0


async def test_lost_response_then_explicit_replay_does_not_duplicate() -> None:
    incidents = IncidentTool(now=lambda: NOW)
    calls = 0

    async def lose_first_response(arguments, context):
        nonlocal calls
        calls += 1
        result = await incidents(arguments, context)
        if calls == 1:
            raise TimeoutError("test-only-transport-detail")
        return result

    registry = registry_for_incident(lose_first_response)
    with pytest.raises(TimeoutError) as caught:
        await registry.execute_claimed_incident(claimed_action())
    assert "test-only-transport-detail" not in str(caught.value)
    assert calls == 1  # Registry does not retry in M1.
    assert incidents.incident_count == 1  # Side effect already exists.
    replayed = await registry.execute_claimed_incident(claimed_action())
    assert replayed.incident_id == "INC-1001"
    assert incidents.incident_count == 1
    assert calls == 2


async def test_invalid_incident_response_does_not_mean_no_side_effect() -> None:
    incidents = IncidentTool(now=lambda: NOW)

    async def malformed_response(arguments, context):
        await incidents(arguments, context)
        return {"status": "created"}  # Required receipt fields are lost.

    registry = registry_for_incident(malformed_response)
    with pytest.raises(ToolOutputValidationError):
        await registry.execute_claimed_incident(claimed_action())
    assert incidents.incident_count == 1


async def test_independent_bundles_do_not_share_incident_ledgers() -> None:
    first = build_mock_tools(DATA_DIR, now=lambda: NOW)
    second = build_mock_tools(DATA_DIR, now=lambda: NOW)
    await first.registry.execute_claimed_incident(claimed_action())
    assert first.incidents.incident_count == 1
    assert second.incidents.incident_count == 0


async def test_read_demo_runs_without_llm_or_incident_creation() -> None:
    result = await run_demo(DATA_DIR)
    assert result["service_status"]["status"] == "degraded"
    assert result["knowledge_matches"]["matches"]
    assert result["incident_request"]["code"] == "approval_required"
    assert result["incident_count"] == 0
