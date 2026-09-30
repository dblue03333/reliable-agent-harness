"""Explicitly opted-in real Gemini calls; incident effects remain synthetic."""

from pathlib import Path

import pytest

from agent_harness.llm.e2e import run_e2e

pytestmark = pytest.mark.live


@pytest.mark.parametrize("scenario", ["read", "approve", "reject"])
async def test_live_api_harness_flow(scenario):
    result = await run_e2e(
        Path(__file__).resolve().parents[2] / "mock_data",
        scenario,
        approve_mock_incident=scenario == "approve",
    )
    assert result["status"] == "passed", result
