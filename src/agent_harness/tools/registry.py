"""Typed allowlist and one-attempt boundary; the harness owns retry/deadline policy."""

from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass

from pydantic import BaseModel, ValidationError

from agent_harness.contracts import ContractModel
from agent_harness.errors import (
    ApprovalRequiredError,
    HarnessError,
    ToolHandlerError,
    ToolInputValidationError,
    ToolOutputValidationError,
    UnknownToolError,
)
from agent_harness.models import ActionStatus, IncidentAction
from agent_harness.tools.context import ToolExecutionContext


@dataclass(frozen=True)
class ToolSpec[InputT: ContractModel, OutputT: ContractModel]:
    name: str
    description: str
    input_schema: type[InputT]
    output_schema: type[OutputT]
    handler: Callable[[InputT, ToolExecutionContext | None], Awaitable[object]]
    side_effect: bool = False
    requires_approval: bool = False
    # Trusted adapter capability, never a claim supplied by the model or user.
    # Enable only when replaying the same action/payload cannot create a second effect.
    supports_incident_replay: bool = False

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("A tool name is required.")
        if self.side_effect and not self.requires_approval:
            raise ValueError("Side-effecting tools must require approval.")
        if self.name == "create_incident" and not (self.side_effect and self.requires_approval):
            raise ValueError("Incident creation must declare side effect and approval.")
        if self.supports_incident_replay and self.name != "create_incident":
            raise ValueError("Only the incident adapter can declare incident replay support.")


@dataclass(frozen=True)
class ValidatedToolCall:
    name: str
    arguments: ContractModel
    requires_approval: bool
    side_effect: bool


def _model_data(value: object) -> object:
    """Rebuild nested model instances too, including ones returned inside raw dictionaries."""
    if isinstance(value, BaseModel):
        value = value.model_dump(warnings=False)
    if isinstance(value, dict):
        return {key: _model_data(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return tuple(_model_data(item) for item in value)
    if isinstance(value, list):
        return [_model_data(item) for item in value]
    return value


class ToolRegistry:
    def __init__(self, specs: Iterable[ToolSpec]) -> None:
        self._specs: dict[str, ToolSpec] = {}
        for spec in specs:
            if spec.name in self._specs:
                raise ValueError("Duplicate tool registration.")
            self._specs[spec.name] = spec

    def _resolve(self, name: str) -> ToolSpec:
        try:
            return self._specs[name]
        except KeyError:
            raise UnknownToolError() from None

    def prepare(self, name: str, arguments: object) -> ValidatedToolCall:
        """Resolve and validate without executing; suitable for making approval proposals."""
        spec = self._resolve(name)
        try:
            if not isinstance(arguments, dict):
                raise ToolInputValidationError()
            typed_arguments = spec.input_schema.model_validate(arguments)
        except ValidationError:
            raise ToolInputValidationError() from None
        return ValidatedToolCall(
            name=spec.name,
            arguments=typed_arguments,
            requires_approval=spec.requires_approval,
            side_effect=spec.side_effect,
        )

    def descriptions(self) -> list[dict]:
        """Fresh, LLM-visible metadata; no handlers or internal context schema."""
        return [
            {
                "name": spec.name,
                "description": spec.description,
                "parameters": spec.input_schema.model_json_schema(),
            }
            for spec in self._specs.values()
        ]

    @property
    def supports_incident_replay(self) -> bool:
        return self._resolve("create_incident").supports_incident_replay

    async def execute(self, name: str, arguments: object) -> ContractModel:
        call = self.prepare(name, arguments)
        if call.requires_approval:
            raise ApprovalRequiredError()
        return await self._dispatch(self._resolve(call.name), call.arguments, context=None)

    async def execute_claimed_incident(self, action: IncidentAction) -> ContractModel:
        """Internal bridge for M4 after atomic claim, not a public approval API.

        Trusted harness code must verify real user approval and store ownership.
        An in-process caller can construct an action: this is not a security sandbox.
        """
        try:
            checked = IncidentAction.model_validate(action.model_dump(warnings=False))
        except ValidationError:
            raise ApprovalRequiredError() from None
        if checked.status != ActionStatus.EXECUTING:
            raise ApprovalRequiredError()
        call = self.prepare(checked.tool, checked.arguments.model_dump())
        return await self._dispatch(
            self._resolve(call.name),
            call.arguments,
            ToolExecutionContext(execution_id=checked.execution_id, action_id=checked.action_id),
        )

    async def _dispatch(
        self, spec: ToolSpec, arguments: ContractModel, context: ToolExecutionContext | None
    ) -> ContractModel:
        try:
            result = await spec.handler(arguments, context)
        except HarnessError:
            raise
        except ValidationError:
            # A typed handler constructing an invalid output may raise here itself.
            raise ToolOutputValidationError() from None
        except TimeoutError:
            # Caller needs timeout identity to reason about ambiguous side effects.
            raise TimeoutError("Tool attempt timed out.") from None
        except Exception:
            raise ToolHandlerError() from None
        # CancelledError inherits BaseException; cancellation is deliberately propagated.
        try:
            result = _model_data(result)
            if not isinstance(result, dict):
                raise ToolOutputValidationError()
            # Rebuild, even for model instances: model_construct/model_copy can bypass validation.
            return spec.output_schema.model_validate(result)
        except (ValueError, TypeError, RecursionError):
            raise ToolOutputValidationError() from None
