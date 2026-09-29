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
    INVALID_MOCK_DATA = "invalid_mock_data"
    APPROVAL_REQUIRED = "approval_required"
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


class UnknownToolError(HarnessError):
    def __init__(self) -> None:
        super().__init__(ErrorCode.UNKNOWN_TOOL, "Tool is not registered.")


class ToolInputValidationError(HarnessError):
    def __init__(self) -> None:
        super().__init__(ErrorCode.INVALID_TOOL_INPUT, "Arguments do not match the tool schema.")


class ToolOutputValidationError(HarnessError):
    """The handler ran; this error does not imply absence of a side effect."""

    def __init__(self) -> None:
        super().__init__(ErrorCode.INVALID_TOOL_OUTPUT, "Result does not match the tool schema.")


class ApprovalRequiredError(HarnessError):
    def __init__(self) -> None:
        super().__init__(
            ErrorCode.APPROVAL_REQUIRED, "This tool requires a claimed approved action."
        )


class MockDataError(HarnessError):
    def __init__(self) -> None:
        super().__init__(ErrorCode.INVALID_MOCK_DATA, "Mock dataset is missing or invalid.")


class PermanentToolError(HarnessError):
    def __init__(self) -> None:
        super().__init__(ErrorCode.TOOL_PERMANENT, "Requested service is not in the mock dataset.")


class TransientToolError(HarnessError):
    def __init__(self) -> None:
        super().__init__(ErrorCode.TOOL_TRANSIENT, "Tool is temporarily unavailable.")


class ToolHandlerError(HarnessError):
    def __init__(self) -> None:
        super().__init__(ErrorCode.INTERNAL_ERROR, "Tool handler raised an unexpected error.")


class IncidentConflictError(HarnessError):
    def __init__(self) -> None:
        super().__init__(
            ErrorCode.ACTION_CONFLICT,
            "Action ID was already used with different incident arguments or execution.",
        )
