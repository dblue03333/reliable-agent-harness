"""Process-local state and ordered events; synchronous mutations on one event loop."""

import asyncio
import logging

from agent_harness.errors import ErrorCode, HarnessError
from agent_harness.models import (
    ActionStatus,
    EventType,
    ExecutionEvent,
    ExecutionState,
    ExecutionStatus,
    IncidentAction,
    Message,
    utc_now,
)

logger = logging.getLogger("agent_harness.events")
_TERMINAL = {ExecutionStatus.COMPLETED, ExecutionStatus.FAILED, ExecutionStatus.LIMIT_EXCEEDED}


class InMemoryStore:
    """Trusted harness-only writes; callers receive detached, validated snapshots.

    Harness entry points hold the per-execution lock for claim/decision only.
    Snapshot writes contain no await and belong to the single winning run owner.
    This is not thread-safe or shared across workers; reads return detached copies.
    """

    def __init__(self) -> None:
        self._states: dict[str, ExecutionState] = {}
        self._events: dict[str, list[ExecutionEvent]] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._log_export_failures = 0

    @property
    def log_export_failures(self) -> int:
        """Process-local count; in-memory events are retained even when export fails."""
        return self._log_export_failures

    def create(self, objective: str, messages: tuple[Message, ...] = ()) -> ExecutionState:
        state = ExecutionState(objective=objective, messages=messages)
        self._states[state.execution_id] = state.model_copy(deep=True)
        self._events[state.execution_id] = []
        self._locks[state.execution_id] = asyncio.Lock()
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

    def lock(self, execution_id: str) -> asyncio.Lock:
        self.get(execution_id)
        return self._locks[execution_id]

    def decide(self, execution_id: str, action_id: str, *, approve: bool) -> ExecutionState:
        """Trusted caller must hold lock(execution_id); no I/O or payload replacement.

        Validate ownership before pending status: missing/foreign IDs are NOT_FOUND,
        while already claimed/resolved/rejected actions are ACTION_CONFLICT.
        """
        state = self.get(execution_id)
        action = next((a for a in state.actions if a.action_id == action_id), None)
        if action is None:
            raise HarnessError(ErrorCode.NOT_FOUND, "Action does not belong to this execution.")
        if (
            state.status != ExecutionStatus.WAITING_APPROVAL
            or state.pending_action_id != action_id
            or action.status != ActionStatus.PENDING
        ):
            raise HarnessError(ErrorCode.ACTION_CONFLICT, "Action is no longer pending approval.")
        decided = IncidentAction.model_validate(
            action.model_dump()
            | {
                "status": ActionStatus.EXECUTING if approve else ActionStatus.REJECTED,
                "decided_at": utc_now(),
            }
        )
        state = self._replace(
            state,
            status=ExecutionStatus.RUNNING,
            pending_action_id=None,
            actions=tuple(decided if a.action_id == action_id else a for a in state.actions),
        )
        self.append_event(
            execution_id,
            EventType.ACTION_CLAIMED if approve else EventType.ACTION_REJECTED,
            action_id=action_id,
            tool=action.tool,
        )
        return state

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
        if next_status not in _TERMINAL | {
            ExecutionStatus.RUNNING,
            ExecutionStatus.WAITING_APPROVAL,
        }:
            raise ValueError("Unsupported execution state transition.")
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
        try:
            logger.info(event.model_dump_json(exclude_none=True))
        except Exception:
            # A failed external sink must not interrupt a committed claim/outcome
            # or terminal cleanup. Do not recursively log through the broken sink.
            self._log_export_failures += 1
        return event
