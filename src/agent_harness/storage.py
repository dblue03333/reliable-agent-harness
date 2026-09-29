"""Process-local state and ordered events; synchronous mutations on one event loop."""

import logging

from agent_harness.errors import ErrorCode, HarnessError
from agent_harness.models import (
    EventType,
    ExecutionEvent,
    ExecutionState,
    ExecutionStatus,
    Message,
    utc_now,
)

logger = logging.getLogger("agent_harness.events")
_TERMINAL = {ExecutionStatus.COMPLETED, ExecutionStatus.FAILED, ExecutionStatus.LIMIT_EXCEEDED}


class InMemoryStore:
    """Trusted harness-only writes; callers receive detached, validated snapshots.

    Claim checks and writes contain no await, so competing tasks on the same event
    loop cannot both claim CREATED. This is not thread-safe or shared across workers.
    Approval/resume will need additional transitions in M4.
    """

    def __init__(self) -> None:
        self._states: dict[str, ExecutionState] = {}
        self._events: dict[str, list[ExecutionEvent]] = {}

    def create(self, objective: str, messages: tuple[Message, ...] = ()) -> ExecutionState:
        state = ExecutionState(objective=objective, messages=messages)
        self._states[state.execution_id] = state.model_copy(deep=True)
        self._events[state.execution_id] = []
        self.append_event(state.execution_id, EventType.EXECUTION_CREATED, status=state.status)
        return self.get(state.execution_id)

    def get(self, execution_id: str) -> ExecutionState:
        try:
            return self._states[execution_id].model_copy(deep=True)
        except KeyError:
            raise HarnessError(ErrorCode.NOT_FOUND, "Execution does not exist.") from None

    def events(self, execution_id: str) -> tuple[ExecutionEvent, ...]:
        self.get(execution_id)
        return tuple(event.model_copy(deep=True) for event in self._events[execution_id])

    def claim(self, execution_id: str) -> ExecutionState:
        state = self.get(execution_id)
        if state.status != ExecutionStatus.CREATED:
            raise HarnessError(ErrorCode.ACTION_CONFLICT, "Execution has already been claimed.")
        return self._replace(state, status=ExecutionStatus.RUNNING)

    def update(self, execution_id: str, **changes: object) -> ExecutionState:
        state = self.get(execution_id)
        if state.status != ExecutionStatus.RUNNING:
            raise HarnessError(ErrorCode.ACTION_CONFLICT, "Only a running execution can advance.")
        if {"execution_id", "objective", "created_at"} & changes.keys():
            raise ValueError("Execution identity cannot change.")
        next_status = changes.get("status", state.status)
        if next_status not in _TERMINAL | {ExecutionStatus.RUNNING}:
            raise ValueError("Unsupported M2 state transition.")
        return self._replace(state, **changes)

    def _replace(self, state: ExecutionState, **changes: object) -> ExecutionState:
        # model_copy(update=...) bypasses validation; always rebuild before committing.
        updated = ExecutionState.model_validate(
            state.model_dump() | changes | {"updated_at": utc_now()}
        )
        self._states[state.execution_id] = updated.model_copy(deep=True)
        if updated.status != state.status:
            self.append_event(
                state.execution_id,
                EventType.STATE_CHANGED,
                status=updated.status,
                termination_reason=updated.termination_reason,
            )
        return self.get(state.execution_id)

    def append_event(
        self, execution_id: str, event_type: EventType, **metadata: object
    ) -> ExecutionEvent:
        state = self.get(execution_id)
        event = ExecutionEvent(
            execution_id=execution_id,
            sequence=len(self._events[execution_id]) + 1,
            step=state.step_count,
            event_type=event_type,
            **metadata,
        )
        self._events[execution_id].append(event.model_copy(deep=True))
        # Only typed metadata is logged. Objective, arguments, raw LLM output and
        # observations remain in the in-memory history, not in operational logs.
        logger.info(event.model_dump_json(exclude_none=True))
        return event
