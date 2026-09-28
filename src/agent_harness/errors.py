"""Stable error codes and safe messages, separate from provider exception text."""

from enum import StrEnum
from typing import Annotated

from pydantic import StringConstraints

from agent_harness.contracts import ContractModel


class ErrorCode(StrEnum):
    INVALID_CONFIGURATION = "invalid_configuration"
    INVALID_DECISION = "invalid_decision"
    UNKNOWN_TOOL = "unknown_tool"
    INVALID_TOOL_INPUT = "invalid_tool_input"
    INVALID_TOOL_OUTPUT = "invalid_tool_output"
    TOOL_TRANSIENT = "tool_transient"
    TOOL_PERMANENT = "tool_permanent"
    TOOL_TIMEOUT = "tool_timeout"
    LLM_ERROR = "llm_error"
    LLM_TIMEOUT = "llm_timeout"
    STEP_LIMIT = "step_limit"
    RUNTIME_LIMIT = "runtime_limit"
    ACTION_CONFLICT = "action_conflict"
    NOT_FOUND = "not_found"
    NOT_DISPATCHED = "not_dispatched"
    OUTCOME_UNKNOWN = "outcome_unknown"
    CANCELLED = "cancelled"
    INTERNAL_ERROR = "internal_error"


class ErrorInfo(ContractModel):
    """Only application-authored messages belong here, never raw SDK errors."""

    code: ErrorCode
    message: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]


class HarnessError(Exception):
    """Internal exception with a stable, safe public representation."""

    def __init__(self, code: ErrorCode, message: str) -> None:
        self.info = ErrorInfo(code=code, message=message)
        super().__init__(self.info.message)


class DecisionValidationError(HarnessError):
    def __init__(self) -> None:
        super().__init__(ErrorCode.INVALID_DECISION, "Response does not match the decision schema.")


class ConfigurationError(HarnessError):
    def __init__(self) -> None:
        super().__init__(
            ErrorCode.INVALID_CONFIGURATION,
            "Invalid application configuration. Check provider credentials, model, and limits.",
        )
