"""M5 recovery and stopping boundaries, tested through real state and mock effects."""

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from agent_harness.config import Settings
from agent_harness.demo import scenario_responses
from agent_harness.errors import (
    DecisionValidationError,
    ErrorCode,
    HarnessError,
    PermanentToolError,
    TransientToolError,
)
from agent_harness.harness import AgentHarness
from agent_harness.llm.fake import ScriptedLLMProvider
from agent_harness.models import (
    ActionOutcome,
    ActionStatus,
    EventType,
    ExecutionStatus,
    TerminationReason,
)
from agent_harness.tools.factory import build_mock_tools
from agent_harness.tools.registry import ToolRegistry, ToolSpec
from agent_harness.tools.schemas import (
    CreateIncidentInput,
    CreateIncidentOutput,
    SearchKnowledgeBaseInput,
    SearchKnowledgeBaseOutput,
)

READ = '{"type":"tool_call","tool":"search_knowledge_base","arguments":{"query":"checkout"}}'
FINAL = '{"type":"final","answer":"Review recorded results."}'
PROPOSAL = scenario_responses("incident-pending")[0]
DATA_DIR = Path(__file__).resolve().parents[1] / "mock_data"


class Clock:
    def __init__(self):
        self.value = 0.0
        self.delays = []

    def __call__(self):
        return self.value

    async def sleep(self, delay):
        self.delays.append(delay)
        self.value += delay
        await asyncio.sleep(0)


def registry_for(handler, *, incident=False, replay=False):
    return ToolRegistry(
        [
            ToolSpec(
                name="create_incident" if incident else "search_knowledge_base",
                description="Fault-injected test adapter",
                input_schema=CreateIncidentInput if incident else SearchKnowledgeBaseInput,
                output_schema=CreateIncidentOutput if incident else SearchKnowledgeBaseOutput,
                handler=handler,
                side_effect=incident,
                requires_approval=incident,
                supports_incident_replay=replay,
            )
        ]
    )


def make(registry=None, responses=(FINAL,), *, clock=None, sleeper=None, **settings):
    clock = clock or Clock()
    provider = AsyncMock()
    provider.generate.side_effect = ScriptedLLMProvider(responses).generate
    harness = AgentHarness(
        provider,
        registry or build_mock_tools(DATA_DIR).registry,
        Settings(**settings),
        clock=clock,
        sleeper=sleeper or clock.sleep,
    )
    return harness, provider, clock


def assert_trace(harness, state):
    events = harness.store.events(state.execution_id)
    assert [e.sequence for e in events] == list(range(1, len(events) + 1))
    assert sum(e.event_type == EventType.EXECUTION_TERMINATED for e in events) == 1
    assert events[-1].event_type == EventType.EXECUTION_TERMINATED
    assert sum(e.event_type == EventType.LLM_REQUESTED for e in events) == state.step_count
    assert sum(e.is_repair is True for e in events) == state.repair_attempt_count
    finished = [e for e in events if e.event_type == EventType.TOOL_FINISHED]
    started = [e for e in events if e.event_type == EventType.TOOL_STARTED]
    assert [e.tool_call_id for e in started] == [r.call_id for r in state.tool_history]
    assert [e.tool_call_id for e in finished] == [r.call_id for r in state.tool_history]
    assert [e.outcome for e in finished] == [r.outcome for r in state.tool_history]
    assert len({r.call_id for r in state.tool_history}) == len(state.tool_history)
    assert all(a.status in (ActionStatus.RESOLVED, ActionStatus.REJECTED) for a in state.actions)
    return events


async def approve(harness):
    waiting = await harness.execute("Propose incident")
    assert waiting.status == ExecutionStatus.WAITING_APPROVAL
    assert not waiting.tool_history
    return await harness.approve(waiting.execution_id, waiting.pending_action_id)


@pytest.mark.parametrize("raw", ["secret malformed", "{}", None, '{"type":"final","answer":""}'])
async def test_one_repair_uses_step_and_safe_feedback(raw, caplog):
    harness, provider, _ = make(responses=(raw, FINAL))
    with caplog.at_level("INFO", logger="agent_harness.events"):
        state = await harness.execute("Inspect")
    assert state.status == ExecutionStatus.COMPLETED
    assert state.step_count == 2
    assert state.repair_attempt_count == 1
    requests = [c.args[0] for c in provider.generate.await_args_list]
    assert requests[0].repair_instruction is None
    assert requests[1].repair_instruction
    assert requests[0].messages == requests[1].messages
    assert "secret malformed" not in caplog.text + state.model_dump_json()
    assert_trace(harness, state)


async def test_adapter_validation_error_uses_same_repair_path():
    harness, provider, _ = make()
    provider.generate.side_effect = [DecisionValidationError(), FINAL]
    state = await harness.execute("Inspect")
    assert state.status == ExecutionStatus.COMPLETED
    assert state.step_count == 2
    assert state.repair_attempt_count == 1
    assert_trace(harness, state)


async def test_repair_exhausted_never_requests_third_response():
    harness, provider, _ = make(responses=("{}", "{}", FINAL))
    state = await harness.execute("Inspect")
    assert state.error.code == ErrorCode.INVALID_DECISION
    assert state.step_count == provider.generate.await_count == 2
    assert state.repair_attempt_count == 1
    assert_trace(harness, state)


@pytest.mark.parametrize("approve_action", [True, False])
async def test_repair_allowance_does_not_reset_after_approval_or_rejection(approve_action):
    harness, provider, clock = make(responses=("{}", PROPOSAL, "{}", FINAL))
    waiting = await harness.execute("Inspect")
    assert waiting.repair_attempt_count == 1
    clock.value += 10000
    resume = harness.approve if approve_action else harness.reject
    state = await resume(waiting.execution_id, waiting.pending_action_id)
    assert state.error.code == ErrorCode.INVALID_DECISION
    assert state.step_count == provider.generate.await_count == 3
    assert state.repair_attempt_count == 1
    assert state.active_runtime_seconds == 0
    if approve_action:
        assert state.actions[0].outcome == ActionOutcome.SUCCEEDED
    else:
        assert state.actions[0].status == ActionStatus.REJECTED
    assert_trace(harness, state)


async def test_repair_step_limit_does_not_spend_unmade_request():
    harness, provider, _ = make(responses=("{}", FINAL), max_agent_steps=1)
    state = await harness.execute("Inspect")
    assert state.error.code == ErrorCode.STEP_LIMIT
    assert state.repair_attempt_count == 0
    assert state.step_count == provider.generate.await_count == 1
    assert_trace(harness, state)


async def test_repair_runtime_is_counted_and_late_valid_final_is_not_accepted():
    harness, provider, clock = make(max_active_runtime_seconds=5)

    async def generate(request):
        clock.value += 3
        return "{}" if request.step == 1 else FINAL

    provider.generate.side_effect = generate
    state = await harness.execute("Inspect")
    assert state.error.code == ErrorCode.RUNTIME_LIMIT
    assert state.final_answer is None
    assert state.active_runtime_seconds == 6
    assert state.repair_attempt_count == 1
    assert_trace(harness, state)


@pytest.mark.parametrize(
    "failure", [HarnessError(ErrorCode.LLM_ERROR, "Unavailable"), TimeoutError()]
)
async def test_provider_transport_failure_does_not_retry_or_repair(failure):
    harness, provider, _ = make()
    provider.generate.side_effect = failure
    state = await harness.execute("Inspect")
    assert state.error.code in (ErrorCode.LLM_ERROR, ErrorCode.LLM_TIMEOUT)
    assert provider.generate.await_count == 1
    assert state.repair_attempt_count == 0
    assert_trace(harness, state)


@pytest.mark.parametrize(
    "raw,code",
    [
        ('{"type":"tool_call","tool":"shell","arguments":{}}', ErrorCode.UNKNOWN_TOOL),
        (READ.replace('"checkout"', '" "'), ErrorCode.INVALID_TOOL_INPUT),
    ],
)
async def test_unknown_tool_and_bad_arguments_are_not_repaired(raw, code):
    harness, provider, _ = make(responses=(raw, FINAL))
    state = await harness.execute("Inspect")
    assert state.error.code == code
    assert state.repair_attempt_count == 0
    assert provider.generate.await_count == 1
    assert not state.tool_history
    assert_trace(harness, state)


@pytest.mark.parametrize("failure", [TransientToolError, TimeoutError])
async def test_read_recovery_records_all_attempts_and_only_one_observation(failure):
    handler = AsyncMock(side_effect=[failure(), failure(), SearchKnowledgeBaseOutput()])
    harness, provider, clock = make(registry_for(handler), (READ, FINAL))
    state = await harness.execute("Inspect")
    assert state.status == ExecutionStatus.COMPLETED
    assert state.step_count == provider.generate.await_count == 2
    assert handler.await_count == 3
    assert [r.attempt for r in state.tool_history] == [1, 2, 3]
    assert [r.outcome for r in state.tool_history] == [
        ActionOutcome.FAILED,
        ActionOutcome.FAILED,
        ActionOutcome.SUCCEEDED,
    ]
    assert clock.delays == [0.25, 0.5]
    assert state.active_runtime_seconds == 0.75
    observations = [m for m in state.messages if m.role == "tool"]
    assert len(observations) == 1
    assert json.loads(observations[0].content)["call_id"] == state.tool_history[-1].call_id
    events = assert_trace(harness, state)
    retries = [e for e in events if e.event_type == EventType.RETRY_SCHEDULED]
    assert [e.attempt for e in retries] == [2, 3]
    assert [e.retry_delay_seconds for e in retries] == clock.delays


@pytest.mark.parametrize("retries", [0, 1, 2])
async def test_read_retry_cap(retries):
    handler = AsyncMock(side_effect=TransientToolError())
    harness, provider, clock = make(
        registry_for(handler),
        (READ, FINAL),
        max_read_tool_retries=retries,
    )
    state = await harness.execute("Inspect")
    assert state.error.code == ErrorCode.TOOL_TRANSIENT
    assert handler.await_count == len(state.tool_history) == retries + 1
    assert len(clock.delays) == retries
    assert provider.generate.await_count == 1
    assert_trace(harness, state)


@pytest.mark.parametrize(
    "failure,output,code",
    [
        (PermanentToolError(), None, ErrorCode.TOOL_PERMANENT),
        (None, {"matches": "invalid"}, ErrorCode.INVALID_TOOL_OUTPUT),
        (RuntimeError("secret exception"), None, ErrorCode.INTERNAL_ERROR),
    ],
)
async def test_non_transient_read_failures_do_not_retry(failure, output, code):
    handler = AsyncMock(side_effect=failure, return_value=output)
    harness, _, clock = make(registry_for(handler), (READ, FINAL))
    state = await harness.execute("Inspect")
    assert state.error.code == code
    assert handler.await_count == 1
    assert clock.delays == []
    assert "secret exception" not in state.model_dump_json()
    assert_trace(harness, state)


@pytest.mark.parametrize("failure", [TransientToolError, TimeoutError])
async def test_lost_incident_response_replays_exact_key_and_creates_one_effect(failure):
    bundle = build_mock_tools(DATA_DIR)
    calls = []

    async def handler(args, context):
        calls.append((args.model_dump(), context))
        result = await bundle.incidents(args, context)
        if len(calls) == 1:
            raise failure()
        return result

    harness, _, clock = make(
        registry_for(handler, incident=True, replay=True),
        (PROPOSAL, FINAL),
    )
    state = await approve(harness)
    assert state.status == ExecutionStatus.COMPLETED
    assert bundle.incidents.incident_count == 1
    assert calls[0] == calls[1]
    assert calls[0][1].action_id == state.actions[0].action_id
    assert [r.outcome for r in state.tool_history] == [
        ActionOutcome.OUTCOME_UNKNOWN,
        ActionOutcome.SUCCEEDED,
    ]
    assert state.actions[0].outcome == ActionOutcome.SUCCEEDED
    assert state.actions[0].result.incident_id == state.tool_history[-1].result["incident_id"]
    assert clock.delays == [0.25]
    assert_trace(harness, state)


@pytest.mark.parametrize("replay,retries,expected", [(False, 1, 1), (True, 0, 1), (True, 1, 2)])
async def test_incident_retry_requires_capability_and_setting(replay, retries, expected):
    bundle = build_mock_tools(DATA_DIR)

    async def lose(args, context):
        await bundle.incidents(args, context)
        raise TimeoutError()

    handler = AsyncMock(side_effect=lose)
    harness, _, clock = make(
        registry_for(handler, incident=True, replay=replay),
        (PROPOSAL, FINAL),
        max_incident_retries=retries,
    )
    state = await approve(harness)
    assert handler.await_count == len(state.tool_history) == expected
    assert len(clock.delays) == expected - 1
    assert bundle.incidents.incident_count == 1
    assert state.actions[0].outcome == ActionOutcome.OUTCOME_UNKNOWN
    assert state.termination_reason == TerminationReason.OUTCOME_UNKNOWN
    assert_trace(harness, state)


@pytest.mark.parametrize(
    "failure,output,code",
    [
        (None, {"incident_id": "bad receipt"}, ErrorCode.INVALID_TOOL_OUTPUT),
        (PermanentToolError(), None, ErrorCode.TOOL_PERMANENT),
        (HarnessError(ErrorCode.ACTION_CONFLICT, "Conflict"), None, ErrorCode.ACTION_CONFLICT),
        (RuntimeError("secret failure"), None, ErrorCode.INTERNAL_ERROR),
    ],
)
async def test_incident_nonretryable_error_does_not_imply_no_effect(failure, output, code):
    bundle = build_mock_tools(DATA_DIR)

    async def handler(args, context):
        await bundle.incidents(args, context)
        if failure:
            raise failure
        return output

    spy = AsyncMock(side_effect=handler)
    harness, _, clock = make(
        registry_for(spy, incident=True, replay=True),
        (PROPOSAL, FINAL),
    )
    state = await approve(harness)
    assert state.error.code == code
    assert state.actions[0].outcome == ActionOutcome.OUTCOME_UNKNOWN
    assert bundle.incidents.incident_count == spy.await_count == 1
    assert not clock.delays
    assert_trace(harness, state)


async def test_failed_retry_preflight_preserves_previous_ambiguity(monkeypatch):
    handler = AsyncMock(side_effect=TimeoutError())
    registry = registry_for(handler, incident=True, replay=True)
    clock = Clock()

    async def sleep(delay):
        await clock.sleep(delay)

        def unavailable(*args):
            raise PermanentToolError()

        monkeypatch.setattr(registry, "prepare", unavailable)

    harness, _, _ = make(registry, (PROPOSAL, FINAL), clock=clock, sleeper=sleep)
    state = await approve(harness)
    assert state.error.code == ErrorCode.TOOL_PERMANENT
    assert state.actions[0].outcome == ActionOutcome.OUTCOME_UNKNOWN
    assert state.actions[0].error.code == ErrorCode.TOOL_TIMEOUT
    assert handler.await_count == len(state.tool_history) == 1
    assert_trace(harness, state)


@pytest.mark.parametrize("incident", [False, True])
async def test_backoff_exhausts_runtime_before_another_dispatch(incident):
    handler = AsyncMock(side_effect=TransientToolError())
    harness, _, clock = make(
        registry_for(handler, incident=incident, replay=incident),
        (PROPOSAL if incident else READ, FINAL),
        max_active_runtime_seconds=0.2,
    )
    state = await approve(harness) if incident else await harness.execute("Inspect")
    assert state.status == ExecutionStatus.LIMIT_EXCEEDED
    assert state.error.code == ErrorCode.RUNTIME_LIMIT
    assert handler.await_count == len(state.tool_history) == 1
    assert state.active_runtime_seconds == 0.25
    if incident:
        assert state.actions[0].outcome == ActionOutcome.OUTCOME_UNKNOWN
    assert_trace(harness, state)


@pytest.mark.parametrize("incident", [False, True])
async def test_cancel_in_backoff_has_no_phantom_attempt_or_abandoned_action(incident):
    entered = asyncio.Event()

    async def sleep(delay):
        entered.set()
        await asyncio.Event().wait()

    handler = AsyncMock(side_effect=TimeoutError())
    harness, _, _ = make(
        registry_for(handler, incident=incident, replay=incident),
        (PROPOSAL if incident else READ, FINAL),
        sleeper=sleep,
    )
    if incident:
        waiting = await harness.execute("Inspect")
        execution_id = waiting.execution_id
        task = asyncio.create_task(harness.approve(execution_id, waiting.pending_action_id))
    else:
        execution_id = harness.create("Inspect").execution_id
        task = asyncio.create_task(harness.run(execution_id))
    await asyncio.wait_for(entered.wait(), 2)
    if incident:
        snapshot = harness.store.get(execution_id)
        assert snapshot.actions[0].status == ActionStatus.EXECUTING
        with pytest.raises(HarnessError) as exc:
            await harness.approve(execution_id, waiting.pending_action_id)
        assert exc.value.info.code == ErrorCode.ACTION_CONFLICT
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    state = harness.store.get(execution_id)
    assert state.termination_reason == TerminationReason.CANCELLED
    assert handler.await_count == len(state.tool_history) == 1
    if incident:
        assert state.actions[0].outcome == ActionOutcome.OUTCOME_UNKNOWN
    assert_trace(harness, state)


@pytest.mark.parametrize("incident", [False, True])
async def test_retry_can_finish_last_step_but_cannot_get_free_summary(incident):
    bundle = build_mock_tools(DATA_DIR)
    handler = AsyncMock(side_effect=[TransientToolError(), SearchKnowledgeBaseOutput()])
    if incident:
        calls = 0

        async def handler(args, context):
            nonlocal calls
            calls += 1
            result = await bundle.incidents(args, context)
            if calls == 1:
                raise TimeoutError()
            return result

    harness, provider, _ = make(
        registry_for(handler, incident=incident, replay=incident),
        (PROPOSAL if incident else READ, FINAL),
        max_agent_steps=1,
    )
    state = await approve(harness) if incident else await harness.execute("Inspect")
    assert state.error.code == ErrorCode.STEP_LIMIT
    assert state.step_count == provider.generate.await_count == 1
    assert state.tool_history[-1].outcome == ActionOutcome.SUCCEEDED
    assert state.final_answer is None
    if incident:
        assert state.actions[0].outcome == ActionOutcome.SUCCEEDED
        assert bundle.incidents.incident_count == 1
    assert_trace(harness, state)


@pytest.mark.parametrize("incident", [False, True])
async def test_real_global_deadline_cancels_backoff(incident):
    entered, cancelled = asyncio.Event(), asyncio.Event()

    async def sleep(delay):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    handler = AsyncMock(side_effect=TimeoutError())
    harness = AgentHarness(
        ScriptedLLMProvider((PROPOSAL if incident else READ, FINAL)),
        registry_for(handler, incident=incident, replay=incident),
        Settings(max_active_runtime_seconds=0.1),
        sleeper=sleep,
    )
    operation = approve(harness) if incident else harness.execute("Inspect")
    state = await asyncio.wait_for(operation, 2)
    assert entered.is_set() and cancelled.is_set()
    assert state.error.code == ErrorCode.RUNTIME_LIMIT
    assert handler.await_count == 1
    if incident:
        assert state.actions[0].outcome == ActionOutcome.OUTCOME_UNKNOWN
    assert_trace(harness, state)


async def test_runtime_exhausted_by_invalid_response_does_not_start_repair():
    harness, provider, clock = make(max_active_runtime_seconds=1)

    async def generate(request):
        clock.value += 1
        return "{}"

    provider.generate.side_effect = generate
    state = await harness.execute("Inspect")
    assert state.error.code == ErrorCode.RUNTIME_LIMIT
    assert state.repair_attempt_count == 0
    assert provider.generate.await_count == 1
    assert_trace(harness, state)


async def test_repair_counter_is_isolated_between_executions():
    harness, _, _ = make(responses=("{}", FINAL))
    states = await asyncio.gather(harness.execute("First"), harness.execute("Second"))
    for state in states:
        assert state.status == ExecutionStatus.COMPLETED
        assert state.repair_attempt_count == 1
        assert state.step_count == 2
        assert_trace(harness, state)


async def test_cancellation_during_repair_is_not_retried():
    entered = asyncio.Event()
    harness, provider, _ = make()

    async def generate(request):
        if request.step == 1:
            return "{}"
        entered.set()
        await asyncio.Event().wait()

    provider.generate.side_effect = generate
    created = harness.create("Inspect")
    task = asyncio.create_task(harness.run(created.execution_id))
    await asyncio.wait_for(entered.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    state = harness.store.get(created.execution_id)
    assert state.termination_reason == TerminationReason.CANCELLED
    assert state.repair_attempt_count == 1
    assert state.step_count == provider.generate.await_count == 2
    assert_trace(harness, state)


async def test_preflight_failure_has_no_attempt_or_side_effect(monkeypatch):
    handler = AsyncMock()
    registry = registry_for(handler, incident=True, replay=True)
    harness, _, _ = make(registry, (PROPOSAL, FINAL))
    waiting = await harness.execute("Inspect")

    def invalid(*args):
        raise PermanentToolError()

    monkeypatch.setattr(registry, "prepare", invalid)
    state = await harness.approve(waiting.execution_id, waiting.pending_action_id)
    assert state.error.code == ErrorCode.TOOL_PERMANENT
    assert state.actions[0].outcome == ActionOutcome.FAILED
    assert state.actions[0].error.code == ErrorCode.NOT_DISPATCHED
    assert not state.tool_history
    handler.assert_not_awaited()
    assert_trace(harness, state)


@pytest.mark.parametrize("incident", [False, True])
async def test_real_operation_timeout_can_recover_within_global_budget(incident):
    bundle = build_mock_tools(DATA_DIR)
    calls = 0
    cancelled = asyncio.Event()

    async def handler(args, context):
        nonlocal calls
        calls += 1
        result = await bundle.incidents(args, context) if incident else SearchKnowledgeBaseOutput()
        if calls == 1:
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        return result

    harness = AgentHarness(
        ScriptedLLMProvider((PROPOSAL if incident else READ, FINAL)),
        registry_for(handler, incident=incident, replay=incident),
        Settings(default_tool_timeout_seconds=0.03, max_active_runtime_seconds=5),
    )
    state = await asyncio.wait_for(approve(harness) if incident else harness.execute("Inspect"), 2)
    assert cancelled.is_set()
    assert calls == 2
    assert state.status == ExecutionStatus.COMPLETED
    assert state.tool_history[0].error.code == ErrorCode.TOOL_TIMEOUT
    assert state.tool_history[-1].outcome == ActionOutcome.SUCCEEDED
    assert state.active_runtime_seconds >= 0.25
    if incident:
        assert state.actions[0].outcome == ActionOutcome.SUCCEEDED
        assert bundle.incidents.incident_count == 1
    assert_trace(harness, state)


@pytest.mark.parametrize("later", ["invalid", "runtime", "llm_error"])
async def test_recovered_incident_receipt_survives_later_failure(later):
    bundle = build_mock_tools(DATA_DIR)
    clock = Clock()
    calls = 0

    async def handler(args, context):
        nonlocal calls
        calls += 1
        result = await bundle.incidents(args, context)
        if calls == 1:
            raise TimeoutError()
        if later == "runtime":
            clock.value += 5
        return result

    harness, provider, _ = make(
        registry_for(handler, incident=True, replay=True),
        (PROPOSAL, "{}", "{}"),
        clock=clock,
        max_active_runtime_seconds=5,
    )
    if later == "llm_error":
        provider.generate.side_effect = [PROPOSAL, RuntimeError("Unavailable")]
    state = await approve(harness)
    assert (
        state.error.code
        == {
            "invalid": ErrorCode.INVALID_DECISION,
            "runtime": ErrorCode.RUNTIME_LIMIT,
            "llm_error": ErrorCode.LLM_ERROR,
        }[later]
    )
    assert state.actions[0].outcome == ActionOutcome.SUCCEEDED
    assert state.tool_history[-1].outcome == ActionOutcome.SUCCEEDED
    assert state.actions[0].result.incident_id == state.tool_history[-1].result["incident_id"]
    assert bundle.incidents.incident_count == 1
    assert_trace(harness, state)


async def test_retry_time_carries_across_approval_pause():
    bundle = build_mock_tools(DATA_DIR)
    original_read = bundle.registry.execute
    original_incident = bundle.registry.execute_claimed_incident
    read_calls, incident_calls = 0, 0

    async def read(name, arguments):
        nonlocal read_calls
        read_calls += 1
        if read_calls == 1:
            raise TransientToolError()
        return await original_read(name, arguments)

    async def incident(action):
        nonlocal incident_calls
        incident_calls += 1
        await original_incident(action)
        raise TimeoutError()

    bundle.registry.execute = read
    bundle.registry.execute_claimed_incident = incident
    harness, provider, clock = make(
        bundle.registry,
        (READ, PROPOSAL, FINAL),
        max_active_runtime_seconds=0.4,
    )
    waiting = await harness.execute("Inspect")
    assert waiting.active_runtime_seconds == 0.25
    assert waiting.step_count == 2
    clock.value += 10000
    state = await harness.approve(waiting.execution_id, waiting.pending_action_id)
    assert state.error.code == ErrorCode.RUNTIME_LIMIT
    assert state.active_runtime_seconds == 0.5
    assert state.step_count == provider.generate.await_count == 2
    assert read_calls == 2 and incident_calls == 1
    assert bundle.incidents.incident_count == 1
    assert state.actions[0].outcome == ActionOutcome.OUTCOME_UNKNOWN
    assert_trace(harness, state)


@pytest.mark.parametrize("during", ["llm", "read", "incident"])
@pytest.mark.parametrize("global_first", [False, True])
async def test_deadline_classification_survives_adapter_exception_translation(during, global_first):
    async def translate_cancellation(*args):
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            # Adapters sometimes wrap cancellation while cleaning up a connection.
            raise TransientToolError() from None

    settings = Settings(
        max_active_runtime_seconds=0.05 if global_first else 5,
        llm_timeout_seconds=5 if global_first else 0.02,
        default_tool_timeout_seconds=5 if global_first else 0.02,
        max_read_tool_retries=0,
        max_incident_retries=0,
    )
    provider = (
        AsyncMock()
        if during == "llm"
        else ScriptedLLMProvider((PROPOSAL if during == "incident" else READ, FINAL))
    )
    if during == "llm":
        provider.generate.side_effect = translate_cancellation
    harness = AgentHarness(
        provider, registry_for(translate_cancellation, incident=during == "incident"), settings
    )
    state = await asyncio.wait_for(
        approve(harness) if during == "incident" else harness.execute("Inspect"), 2
    )
    assert state.error.code == (
        ErrorCode.RUNTIME_LIMIT
        if global_first
        else ErrorCode.LLM_TIMEOUT
        if during == "llm"
        else ErrorCode.TOOL_TIMEOUT
    )
    assert state.status == (
        ExecutionStatus.LIMIT_EXCEEDED if global_first else ExecutionStatus.FAILED
    )
    if during == "incident":
        assert state.actions[0].outcome == ActionOutcome.OUTCOME_UNKNOWN
    assert_trace(harness, state)


@pytest.mark.parametrize("incident", [False, True])
@pytest.mark.parametrize("with_previous_read", [False, True])
async def test_preflight_transient_error_cannot_borrow_an_old_attempt(
    monkeypatch, incident, with_previous_read
):
    bundle = build_mock_tools(DATA_DIR)
    original_prepare = bundle.registry.prepare
    calls = 0
    responses = ((READ,) if with_previous_read else ()) + (
        PROPOSAL if incident else READ,
        FINAL,
    )
    # Each successful read prepares at proposal, attempt preflight, and registry dispatch.
    fail_at = (3 if with_previous_read else 0) + 2

    def preflight(name, arguments):
        nonlocal calls
        calls += 1
        if calls == fail_at:
            raise TransientToolError()
        return original_prepare(name, arguments)

    monkeypatch.setattr(bundle.registry, "prepare", preflight)
    harness, _, clock = make(bundle.registry, responses)
    state = await harness.execute("Inspect")
    if incident:
        state = await harness.approve(state.execution_id, state.pending_action_id)
    assert state.error.code == ErrorCode.TOOL_TRANSIENT
    assert len(state.tool_history) == int(with_previous_read)
    assert bundle.incidents.incident_count == 0
    assert not clock.delays
    events = assert_trace(harness, state)
    assert all(e.event_type != EventType.RETRY_SCHEDULED for e in events)
    if incident:
        assert state.actions[0].error.code == ErrorCode.NOT_DISPATCHED


@pytest.mark.parametrize("returns", [False, True])
async def test_global_budget_exhausted_during_local_timeout_cleanup_takes_precedence(returns):
    clock = Clock()

    async def generate(request):
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            clock.value += 10
            if returns:
                return FINAL
            raise TransientToolError() from None

    provider = AsyncMock()
    provider.generate.side_effect = generate
    harness = AgentHarness(
        provider,
        build_mock_tools(DATA_DIR).registry,
        Settings(llm_timeout_seconds=0.02, max_active_runtime_seconds=10),
        clock=clock,
    )
    state = await asyncio.wait_for(harness.execute("Inspect"), 2)
    assert state.status == ExecutionStatus.LIMIT_EXCEEDED
    assert state.error.code == ErrorCode.RUNTIME_LIMIT
    assert state.final_answer is None
    assert_trace(harness, state)
