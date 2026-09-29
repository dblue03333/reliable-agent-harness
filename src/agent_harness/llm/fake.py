"""Deterministic raw responses selected by execution-local request.step."""

from collections.abc import Sequence

from agent_harness.errors import ErrorCode, HarnessError
from agent_harness.llm.base import LLMRequest


class ScriptedLLMProvider:
    """A test/demo provider, not an objective-understanding language model.

    No global cursor: the harness-owned step indexes an immutable script, so
    concurrent executions sharing this provider each start at response one.
    Raw strings are intentional; tests can supply malformed LLM responses.
    """

    def __init__(self, responses: Sequence[str], *, repeat_last: bool = False) -> None:
        if isinstance(responses, str) or not responses:
            raise ValueError("A nonempty sequence of raw responses is required.")
        self._responses = tuple(responses)
        self._repeat_last = repeat_last

    async def generate(self, request: LLMRequest) -> str:
        index = request.step - 1
        if index < len(self._responses):
            return self._responses[index]
        if self._repeat_last:
            return self._responses[-1]
        raise HarnessError(ErrorCode.LLM_ERROR, "Fake response script exhausted.")
