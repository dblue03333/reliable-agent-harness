"""Approval ownership, races, exact payload, budgets and side-effect cleanup."""

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from agent_harness.config import Settings
from agent_harness.demo import scenario_responses
from agent_harness.errors import ErrorCode, HarnessError
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
from agent_harness.tools.schemas import CreateIncidentInput, CreateIncidentOutput

FINAL = '{"type":"final","answer":"Review the recorded action outcome."}'
PROPOSAL = scenario_responses("incident-blocked")[0]
DATA_DIR = Path(__file__).resolve().parents[1] / "mock_data"


@pytest.fixture
def bundle():
    return build_mock_tools(DATA_DIR)


def make_harness(bundle, *, responses=(PROPOSAL, FINAL), provider=None, **settings):
    return AgentHarness(
        provider or ScriptedLLMProvider(responses), bundle.registry, Settings(**settings)
    )


def incident_registry(handler):
    return ToolRegistry(
        [
            ToolSpec(
                name="create_incident",
                description="Mock incident",
                input_schema=CreateIncidentInput,
                output_schema=CreateIncidentOutput,
                handler=handler,
                side_effect=True,
                requires_approval=True,
            )
        ]
    )


def assert_settled(harness, state):
    assert state.status in (
        ExecutionStatus.COMPLETED,
        ExecutionStatus.FAILED,
        ExecutionStatus.LIMIT_EXCEEDED,
    )
    assert state.pending_action_id is None
    assert all(
        a.status not in (ActionStatus.PENDING, ActionStatus.EXECUTING) for a in state.actions
    )
    events = harness.store.events(state.execution_id)
    assert [e.sequence for e in events] == list(range(1, len(events) + 1))
    assert sum(e.event_type == EventType.EXECUTION_TERMINATED for e in events) == 1
    assert events[-1].event_type == EventType.EXECUTION_TERMINATED
    finished = [e for e in events if e.event_type == EventType.TOOL_FINISHED]
    assert [e.tool_call_id for e in finished] == [r.call_id for r in state.tool_history]


async def test_pause_then_approve_executes_exact_snapshot_and_feeds_result_to_llm(bundle):
    provider = AsyncMock()
    provider.generate.side_effect = ScriptedLLMProvider((PROPOSAL, FINAL)).generate
    harness = make_harness(bundle, provider=provider)
    waiting = await harness.execute("Investigate and propose an incident")
    action = waiting.actions[0]
    assert waiting.status == ExecutionStatus.WAITING_APPROVAL
    assert waiting.pending_action_id == action.action_id
    assert waiting.tool_history == ()
    assert provider.generate.await_count == 1
    assert bundle.incidents.incident_count == 0
    assert waiting.error is None
    assert waiting.termination_reason is None
    assert not any(
        e.event_type == EventType.EXECUTION_TERMINATED
        for e in harness.store.events(waiting.execution_id)
    )
    original = action.arguments.model_dump()
    # Even a caller deliberately bypassing frozen-model validation changes only its copy.
    action.arguments.__dict__["title"] = "tampered snapshot"
    awaiting = harness.store.get(waiting.execution_id)
    assert awaiting.actions[0].arguments.model_dump() == original
    spy = AsyncMock(wraps=bundle.registry.execute_claimed_incident)
    bundle.registry.execute_claimed_incident = spy
    state = await harness.approve(waiting.execution_id, waiting.pending_action_id)
    assert state.status == ExecutionStatus.COMPLETED
    assert state.step_count == 2
    assert state.actions[0].outcome == ActionOutcome.SUCCEEDED
    assert bundle.incidents.incident_count == 1
    dispatched = spy.await_args.args[0]
    assert dispatched.arguments.model_dump() == original
    assert dispatched.action_id == waiting.pending_action_id
    assert dispatched.execution_id == waiting.execution_id
    assert state.tool_history[0].action_id == dispatched.action_id
    last_request = provider.generate.await_args_list[-1].args[0]
    observation = json.loads(last_request.messages[-1].content)
    assert observation["result"]["incident_id"] == state.actions[0].result.incident_id
    assert observation["action_id"] == dispatched.action_id
    assert_settled(harness, state)


async def test_reject_records_denial_and_resumes_without_tool_call(bundle):
    provider = AsyncMock()
    provider.generate.side_effect = ScriptedLLMProvider((PROPOSAL, FINAL)).generate
    harness = make_harness(bundle, provider=provider)
    waiting = await harness.execute("Propose incident")
    state = await harness.reject(waiting.execution_id, waiting.pending_action_id)
    assert state.status == ExecutionStatus.COMPLETED
    assert state.actions[0].status == ActionStatus.REJECTED
    assert state.actions[0].outcome is None
    assert state.actions[0].decided_at is not None
    assert state.tool_history == ()
    assert bundle.incidents.incident_count == 0
    observation = json.loads(provider.generate.await_args_list[-1].args[0].messages[-1].content)
    assert observation == {
        "tool": "create_incident",
        "action_id": waiting.pending_action_id,
        "status": "rejected",
    }
    assert_settled(harness, state)


@pytest.mark.parametrize("initial", ["approve", "reject"])
@pytest.mark.parametrize("duplicate", ["approve", "reject", "run"])
async def test_sequential_duplicate_never_resumes_again(bundle, initial, duplicate):
    harness = make_harness(bundle)
    waiting = await harness.execute("Propose")
    state = await getattr(harness, initial)(waiting.execution_id, waiting.pending_action_id)
    with pytest.raises(HarnessError) as exc:
        if duplicate == "run":
            await harness.run(waiting.execution_id)
        else:
            await getattr(harness, duplicate)(waiting.execution_id, waiting.pending_action_id)
    assert exc.value.info.code == ErrorCode.ACTION_CONFLICT
    assert harness.store.get(waiting.execution_id) == state
    assert bundle.incidents.incident_count == (1 if initial == "approve" else 0)


async def test_foreign_missing_and_forged_action_cannot_authorize(bundle):
    harness = make_harness(bundle)
    first, second = await asyncio.gather(harness.execute("First"), harness.execute("Second"))
    for execution_id, action_id in [
        (first.execution_id, second.pending_action_id),
        (first.execution_id, "act_missing"),
        ("exec_missing", first.pending_action_id),
    ]:
        with pytest.raises(HarnessError) as exc:
            await harness.approve(execution_id, action_id)
        assert exc.value.info.code == ErrorCode.NOT_FOUND
    with pytest.raises(TypeError):
        await harness.approve(first.execution_id, first.pending_action_id, arguments={"title": "x"})
    with pytest.raises(HarnessError):
        await harness.run(first.execution_id)
    assert harness.store.get(first.execution_id) == first
    assert bundle.incidents.incident_count == 0


@pytest.mark.parametrize(
    ("winner", "loser"),
    [("approve", "approve"), ("approve", "reject"), ("reject", "approve"), ("reject", "reject")],
)
async def test_concurrent_decisions_have_one_resume_owner(bundle, winner, loser):
    entered, release = asyncio.Event(), asyncio.Event()

    async def generate(request):
        if request.step == 1:
            return PROPOSAL
        entered.set()
        await release.wait()
        return FINAL

    provider = AsyncMock()
    provider.generate.side_effect = generate
    harness = make_harness(bundle, provider=provider)
    waiting = await harness.execute("Propose")
    task = asyncio.create_task(
        getattr(harness, winner)(waiting.execution_id, waiting.pending_action_id)
    )
    await asyncio.wait_for(entered.wait(), 2)
    try:
        assert not harness.store.lock(waiting.execution_id).locked()
        with pytest.raises(HarnessError) as exc:
            await getattr(harness, loser)(waiting.execution_id, waiting.pending_action_id)
        assert exc.value.info.code == ErrorCode.ACTION_CONFLICT
        assert harness.store.get(waiting.execution_id).status == ExecutionStatus.RUNNING
    finally:
        release.set()
    state = await task
    assert provider.generate.await_count == 2
    assert bundle.incidents.incident_count == (1 if winner == "approve" else 0)
    assert_settled(harness, state)


async def test_duplicate_approval_during_incident_io_is_rejected(bundle):
    entered, release = asyncio.Event(), asyncio.Event()

    async def handler(args, context):
        entered.set()
        await release.wait()
        return await bundle.incidents(args, context)

    harness = AgentHarness(
        ScriptedLLMProvider((PROPOSAL, FINAL)), incident_registry(handler), Settings()
    )
    waiting = await harness.execute("Propose")
    task = asyncio.create_task(harness.approve(waiting.execution_id, waiting.pending_action_id))
    await asyncio.wait_for(entered.wait(), 2)
    try:
        assert harness.store.get(waiting.execution_id).actions[0].status == ActionStatus.EXECUTING
        assert not harness.store.lock(waiting.execution_id).locked()
        with pytest.raises(HarnessError):
            await harness.approve(waiting.execution_id, waiting.pending_action_id)
    finally:
        release.set()
    state = await task
    assert bundle.incidents.incident_count == 1
    assert len(state.tool_history) == 1
    assert_settled(harness, state)


async def test_cancel_while_waiting_for_claim_leaves_pending_action_untouched(bundle):
    harness = make_harness(bundle)
    waiting = await harness.execute("Propose")
    async with harness.store.lock(waiting.execution_id):
        task = asyncio.create_task(harness.approve(waiting.execution_id, waiting.pending_action_id))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert harness.store.get(waiting.execution_id) == waiting
    assert bundle.incidents.incident_count == 0
    state = await harness.reject(waiting.execution_id, waiting.pending_action_id)
    assert_settled(harness, state)


async def test_cancel_immediately_after_claim_settles_without_dispatch(bundle, monkeypatch):
    harness = make_harness(bundle)
    waiting = await harness.execute("Propose")
    original = harness.store.decide

    def decide(*args, **kwargs):
        state = original(*args, **kwargs)
        asyncio.current_task().cancel()
        return state

    monkeypatch.setattr(harness.store, "decide", decide)
    task = asyncio.create_task(harness.approve(waiting.execution_id, waiting.pending_action_id))
    with pytest.raises(asyncio.CancelledError):
        await task
    state = harness.store.get(waiting.execution_id)
    assert state.termination_reason == TerminationReason.CANCELLED
    assert state.actions[0].outcome == ActionOutcome.FAILED
    assert state.actions[0].error.code == ErrorCode.NOT_DISPATCHED
    assert state.tool_history == ()
    assert bundle.incidents.incident_count == 0
    assert_settled(harness, state)


@pytest.mark.parametrize("effect_before_cancel", [False, True])
async def test_cancel_during_incident_is_unknown_without_abandoned_executing(
    bundle, effect_before_cancel
):
    entered = asyncio.Event()

    async def handler(args, context):
        if effect_before_cancel:
            await bundle.incidents(args, context)
        entered.set()
        await asyncio.Event().wait()

    harness = AgentHarness(
        ScriptedLLMProvider((PROPOSAL, FINAL)), incident_registry(handler), Settings()
    )
    waiting = await harness.execute("Propose")
    task = asyncio.create_task(harness.approve(waiting.execution_id, waiting.pending_action_id))
    await asyncio.wait_for(entered.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    state = harness.store.get(waiting.execution_id)
    assert state.termination_reason == TerminationReason.CANCELLED
    assert state.actions[0].outcome == ActionOutcome.OUTCOME_UNKNOWN
    assert state.tool_history[0].outcome == ActionOutcome.OUTCOME_UNKNOWN
    assert bundle.incidents.incident_count == int(effect_before_cancel)
    assert_settled(harness, state)


@pytest.mark.parametrize("failure", ["timeout", "invalid_output", "exception", "global_timeout"])
async def test_effect_before_error_stays_unknown_and_never_retried(bundle, failure):
    calls = 0

    async def handler(args, context):
        nonlocal calls
        calls += 1
        await bundle.incidents(args, context)
        if failure in ("timeout", "global_timeout"):
            await asyncio.Event().wait()
        if failure == "invalid_output":
            return {"incident_id": "missing timestamp"}
        raise RuntimeError("secret adapter diagnostic")

    settings = Settings(
        default_tool_timeout_seconds=5 if failure == "global_timeout" else 0.03,
        max_active_runtime_seconds=0.03 if failure == "global_timeout" else 60,
    )
    harness = AgentHarness(
        ScriptedLLMProvider((PROPOSAL, FINAL)), incident_registry(handler), settings
    )
    waiting = await harness.execute("Propose")
    state = await asyncio.wait_for(
        harness.approve(waiting.execution_id, waiting.pending_action_id), 2
    )
    assert state.actions[0].outcome == ActionOutcome.OUTCOME_UNKNOWN
    assert state.actions[0].result is None
    assert state.tool_history[0].outcome == ActionOutcome.OUTCOME_UNKNOWN
    assert state.termination_reason == (
        TerminationReason.ACTIVE_RUNTIME_EXHAUSTED
        if failure == "global_timeout"
        else TerminationReason.OUTCOME_UNKNOWN
    )
    assert calls == bundle.incidents.incident_count == 1
    assert "secret" not in state.model_dump_json()
    assert_settled(harness, state)


@pytest.mark.parametrize("later", ["provider_error", "cancel", "step_limit"])
async def test_incident_success_survives_later_summary_failure(bundle, later):
    entered = asyncio.Event()

    async def generate(request):
        if request.step == 1:
            return PROPOSAL
        entered.set()
        if later == "cancel":
            await asyncio.Event().wait()
        raise RuntimeError("summary unavailable")

    provider = AsyncMock()
    provider.generate.side_effect = generate
    harness = make_harness(
        bundle, provider=provider, max_agent_steps=1 if later == "step_limit" else 10
    )
    waiting = await harness.execute("Propose")
    task = asyncio.create_task(harness.approve(waiting.execution_id, waiting.pending_action_id))
    if later == "cancel":
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        state = harness.store.get(waiting.execution_id)
    else:
        state = await task
    assert state.actions[0].outcome == ActionOutcome.SUCCEEDED
    assert state.actions[0].result.incident_id == state.tool_history[0].result["incident_id"]
    assert bundle.incidents.incident_count == 1
    assert state.status != ExecutionStatus.COMPLETED
    assert_settled(harness, state)


class Clock:
    value = 0.0

    def __call__(self):
        return self.value


async def test_waiting_time_excluded_and_runtime_accumulates_over_resume(bundle):
    clock = Clock()

    async def generate(request):
        clock.value += 2
        return PROPOSAL if request.step == 1 else FINAL

    provider = AsyncMock()
    provider.generate.side_effect = generate
    harness = AgentHarness(
        provider, bundle.registry, Settings(max_active_runtime_seconds=10), clock=clock
    )
    waiting = await harness.execute("Propose")
    assert waiting.active_runtime_seconds == 2
    clock.value += 86400  # One day of human waiting must be free.
    state = await harness.approve(waiting.execution_id, waiting.pending_action_id)
    assert state.status == ExecutionStatus.COMPLETED
    assert state.active_runtime_seconds == 4
    assert state.step_count == 2
    assert_settled(harness, state)


async def test_runtime_exhaustion_between_claim_and_dispatch_never_calls_handler(
    bundle, monkeypatch
):
    clock = Clock()
    harness = AgentHarness(
        ScriptedLLMProvider((PROPOSAL, FINAL)),
        bundle.registry,
        Settings(max_active_runtime_seconds=5),
        clock=clock,
    )
    waiting = await harness.execute("Propose")
    original = harness._incident_tool

    async def advance_clock(*args):
        clock.value += 5
        await original(*args)

    monkeypatch.setattr(harness, "_incident_tool", advance_clock)
    state = await harness.approve(waiting.execution_id, waiting.pending_action_id)
    assert state.status == ExecutionStatus.LIMIT_EXCEEDED
    assert state.error.code == ErrorCode.RUNTIME_LIMIT
    assert state.actions[0].error.code == ErrorCode.NOT_DISPATCHED
    assert state.tool_history == ()
    assert bundle.incidents.incident_count == 0
    assert_settled(harness, state)


async def test_reproposal_needs_new_approval_and_old_id_cannot_approve_it(bundle):
    harness = make_harness(bundle, responses=(PROPOSAL, PROPOSAL, FINAL))
    first = await harness.execute("Propose")
    second = await harness.reject(first.execution_id, first.pending_action_id)
    assert second.status == ExecutionStatus.WAITING_APPROVAL
    assert second.pending_action_id != first.pending_action_id
    assert second.step_count == 2
    with pytest.raises(HarnessError) as exc:
        await harness.approve(second.execution_id, first.pending_action_id)
    assert exc.value.info.code == ErrorCode.ACTION_CONFLICT
    state = await harness.approve(second.execution_id, second.pending_action_id)
    assert state.status == ExecutionStatus.COMPLETED
    assert [a.status for a in state.actions] == [ActionStatus.REJECTED, ActionStatus.RESOLVED]
    assert bundle.incidents.incident_count == 1
    assert_settled(harness, state)


async def test_repeated_rejections_consume_steps_and_eventually_stop(bundle):
    harness = make_harness(
        bundle, provider=ScriptedLLMProvider((PROPOSAL,), repeat_last=True), max_agent_steps=2
    )
    state = await harness.execute("Propose")
    for _ in range(2):
        state = await harness.reject(state.execution_id, state.pending_action_id)
    assert state.status == ExecutionStatus.LIMIT_EXCEEDED
    assert state.step_count == 2
    assert len(state.actions) == 2
    assert bundle.incidents.incident_count == 0
    assert_settled(harness, state)


async def test_simultaneous_approve_reject_contend_on_same_lock(bundle):
    harness = make_harness(bundle)
    waiting = await harness.execute("Propose")
    async with harness.store.lock(waiting.execution_id):
        approval = asyncio.create_task(
            harness.approve(waiting.execution_id, waiting.pending_action_id)
        )
        rejection = asyncio.create_task(
            harness.reject(waiting.execution_id, waiting.pending_action_id)
        )
        await asyncio.sleep(0)
        assert not approval.done() and not rejection.done()
    results = await asyncio.gather(approval, rejection, return_exceptions=True)
    errors = [r for r in results if isinstance(r, HarnessError)]
    assert len(errors) == 1
    assert errors[0].info.code == ErrorCode.ACTION_CONFLICT
    state = harness.store.get(waiting.execution_id)
    assert state.step_count == 2
    assert bundle.incidents.incident_count == int(state.actions[0].status == ActionStatus.RESOLVED)
    assert_settled(harness, state)


async def test_distinct_executions_approve_without_crossing_payloads_or_keys(bundle):
    captured = []
    original = bundle.registry.execute_claimed_incident

    async def observe(action):
        captured.append((action.execution_id, action.action_id, action.arguments.title))
        await asyncio.sleep(0)
        return await original(action)

    bundle.registry.execute_claimed_incident = observe
    first = make_harness(bundle)
    altered = json.loads(PROPOSAL)
    altered["arguments"]["title"] = "Different incident"
    second = make_harness(bundle, responses=(json.dumps(altered), FINAL))
    a, b = await asyncio.gather(first.execute("First"), second.execute("Second"))
    one, two = await asyncio.gather(
        first.approve(a.execution_id, a.pending_action_id),
        second.approve(b.execution_id, b.pending_action_id),
    )
    assert bundle.incidents.incident_count == 2
    assert one.actions[0].result.incident_id != two.actions[0].result.incident_id
    assert set(captured) == {
        (a.execution_id, a.pending_action_id, a.actions[0].arguments.title),
        (b.execution_id, b.pending_action_id, b.actions[0].arguments.title),
    }
    assert_settled(first, one)
    assert_settled(second, two)


async def test_invalid_incident_arguments_do_not_create_an_approvable_action(bundle):
    invalid = json.loads(PROPOSAL)
    invalid["arguments"]["approved"] = True
    harness = make_harness(bundle, responses=(json.dumps(invalid),))
    state = await harness.execute("Propose")
    assert state.error.code == ErrorCode.INVALID_TOOL_INPUT
    assert state.actions == ()
    assert bundle.incidents.incident_count == 0
    assert_settled(harness, state)


async def test_incident_success_survives_runtime_exhaustion_before_summary(bundle):
    clock = Clock()

    async def handler(args, context):
        result = await bundle.incidents(args, context)
        clock.value += 5
        return result

    harness = AgentHarness(
        ScriptedLLMProvider((PROPOSAL, FINAL)),
        incident_registry(handler),
        Settings(max_active_runtime_seconds=5),
        clock=clock,
    )
    waiting = await harness.execute("Propose")
    state = await harness.approve(waiting.execution_id, waiting.pending_action_id)
    assert state.status == ExecutionStatus.LIMIT_EXCEEDED
    assert state.error.code == ErrorCode.RUNTIME_LIMIT
    assert state.actions[0].outcome == ActionOutcome.SUCCEEDED
    assert bundle.incidents.incident_count == 1
    assert state.step_count == 1
    assert_settled(harness, state)


@pytest.mark.parametrize(
    "event_type", ["state_changed", "action_claimed", "tool_finished", "execution_terminated"]
)
async def test_log_export_failure_cannot_interrupt_claim_or_outcome(bundle, event_type):
    import logging

    class BrokenSink(logging.Handler):
        def emit(self, record):
            event = json.loads(record.getMessage())
            if event["event_type"] == event_type:
                raise OSError("log destination unavailable")

    harness = make_harness(bundle)
    waiting = await harness.execute("Propose")
    logger = logging.getLogger("agent_harness.events")
    sink = BrokenSink()
    old_level = logger.level
    logger.setLevel(logging.INFO)
    logger.addHandler(sink)
    try:
        state = await harness.approve(waiting.execution_id, waiting.pending_action_id)
    finally:
        logger.removeHandler(sink)
        logger.setLevel(old_level)
    assert state.status == ExecutionStatus.COMPLETED
    assert state.actions[0].outcome == ActionOutcome.SUCCEEDED
    assert bundle.incidents.incident_count == 1
    assert harness.store.log_export_failures > 0
    assert_settled(harness, state)


@pytest.mark.parametrize("operation", ["approve", "reject"])
async def test_missing_action_id_never_becomes_initial_run(bundle, operation):
    harness = make_harness(bundle)
    created = harness.create("Propose")
    with pytest.raises(HarnessError) as exc:
        await getattr(harness, operation)(created.execution_id, None)
    assert exc.value.info.code == ErrorCode.NOT_FOUND
    assert harness.store.get(created.execution_id) == created
    assert bundle.incidents.incident_count == 0
