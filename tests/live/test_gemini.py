"""Two real provider calls, only when RUN_LIVE_TESTS=1 is explicitly set."""

from pathlib import Path

import pytest

from agent_harness.llm.smoke import run_smoke

pytestmark = pytest.mark.live


async def test_live_tool_and_final_responses_match_current_contracts():
    result = await run_smoke(Path(__file__).resolve().parents[2] / "mock_data")
    assert result["status"] == "passed"
    assert result["validated_decisions"] == ["tool_call", "final"]
    assert result["tool_dispatches"] == 0
    assert result["model"]
