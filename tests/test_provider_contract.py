"""An interchangeable async provider can return even malformed output for harness validation."""

import pytest

from agent_harness.errors import DecisionValidationError
from agent_harness.llm.base import LLMProvider, LLMRequest
from agent_harness.models import FinalDecision, parse_decision


class StubProvider:
    def __init__(self, response: str) -> None:
        self.response = response
        self.requests: list[LLMRequest] = []

    async def generate(self, request: LLMRequest) -> str:
        self.requests.append(request)
        return self.response


async def test_provider_proposes_raw_json_and_consumer_owns_validation() -> None:
    stub = StubProvider('{"type":"final","answer":"Checkout is degraded."}')
    provider: LLMProvider = stub
    request = LLMRequest(execution_id="exec_1", objective="Investigate checkout", step=1)
    decision = parse_decision(await provider.generate(request))
    assert isinstance(decision, FinalDecision)
    assert stub.requests == [request]

    malformed: LLMProvider = StubProvider("not JSON")
    raw = await malformed.generate(request)
    with pytest.raises(DecisionValidationError):
        parse_decision(raw)
