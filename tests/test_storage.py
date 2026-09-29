"""Snapshots, legal transitions and detached state at the in-memory boundary."""

import pytest
from pydantic import ValidationError

from agent_harness.errors import ErrorCode, HarnessError
from agent_harness.models import ActionOutcome, ExecutionStatus, TerminationReason, ToolCallRecord
from agent_harness.storage import InMemoryStore


def test_store_validates_before_mutating_and_rejects_invalid_transitions():
    store = InMemoryStore()
    created = store.create("Investigate checkout")
    with pytest.raises(HarnessError):
        store.update(created.execution_id, step_count=1)
    running = store.claim(created.execution_id)
    with pytest.raises(ValidationError):
        store.update(created.execution_id, step_count=-1)
    with pytest.raises(ValueError):
        store.update(created.execution_id, objective="replacement")
    with pytest.raises(ValueError):
        store.update(created.execution_id, status=ExecutionStatus.WAITING_APPROVAL)
    assert store.get(created.execution_id) == running
    assert len(store.events(created.execution_id)) == 2
    with pytest.raises(HarnessError) as exc:
        store.claim(created.execution_id)
    assert exc.value.info.code == ErrorCode.ACTION_CONFLICT


def test_nested_results_are_detached_on_write_and_read():
    store = InMemoryStore()
    state = store.create("Inspect checkout")
    store.claim(state.execution_id)
    record = ToolCallRecord(
        call_id="call_1",
        tool="read",
        step=1,
        attempt=1,
        outcome=ActionOutcome.SUCCEEDED,
        duration_ms=0.0,
        result={"nested": {"values": [1]}},
    )
    state = store.update(state.execution_id, tool_history=(record,))
    record.result["nested"]["values"].append(2)
    state.tool_history[0].result["nested"]["values"].append(3)
    assert store.get(state.execution_id).tool_history[0].result == {"nested": {"values": [1]}}


def test_terminal_execution_cannot_be_rewritten_or_claimed():
    store = InMemoryStore()
    state = store.create("Inspect")
    store.claim(state.execution_id)
    state = store.update(
        state.execution_id,
        status=ExecutionStatus.COMPLETED,
        final_answer="Done",
        termination_reason=TerminationReason.FINAL_ANSWER,
    )
    with pytest.raises(HarnessError):
        store.update(state.execution_id, final_answer="Different answer")
    with pytest.raises(HarnessError):
        store.claim(state.execution_id)
    assert store.get(state.execution_id) == state


def test_missing_execution_is_typed_and_events_do_not_leak_internal_list():
    store = InMemoryStore()
    for method in (store.get, store.events, store.claim):
        with pytest.raises(HarnessError) as exc:
            method("missing")
        assert exc.value.info.code == ErrorCode.NOT_FOUND
    state = store.create("Inspect")
    old_events = store.events(state.execution_id)
    store.claim(state.execution_id)
    assert len(old_events) == 1
    assert [e.sequence for e in store.events(state.execution_id)] == [1, 2]
