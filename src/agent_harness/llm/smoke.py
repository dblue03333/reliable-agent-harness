"""Opt-in Gemini schema smoke: two real responses, validation only, no tool dispatch."""

import argparse
import asyncio
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from google import genai

from agent_harness.budget import ActiveBudget
from agent_harness.config import Settings, load_settings
from agent_harness.errors import ConfigurationError, ErrorCode, HarnessError
from agent_harness.llm.base import LLMProvider, LLMRequest
from agent_harness.llm.gemini import GeminiLLMProvider
from agent_harness.llm.schema import gemini_decision_schema
from agent_harness.models import FinalDecision, Message, ToolCallDecision, parse_decision
from agent_harness.tools.factory import build_mock_tools
from agent_harness.tools.registry import ToolRegistry


async def check_schema_smoke(
    provider: LLMProvider, registry: ToolRegistry, settings: Settings
) -> dict:
    budget = ActiveBudget(settings.max_active_runtime_seconds)
    execution_id = f"smoke_{uuid4().hex}"
    cases = (
        (
            "tool_call",
            "Return a tool_call decision proposing get_service_status for checkout-api. "
            "Only propose it; do not claim that the tool has been executed.",
        ),
        (
            "final",
            "Return a final decision acknowledging this schema smoke test. "
            "No tools were executed; do not claim otherwise.",
        ),
    )
    verified = []
    for step, (expected_type, objective) in enumerate(cases, start=1):
        budget.check()
        if step > settings.max_agent_steps:
            raise HarnessError(ErrorCode.STEP_LIMIT, "Schema smoke needs two LLM steps.")
        request = LLMRequest(
            execution_id=execution_id,
            objective=objective,
            step=step,
            messages=(
                Message(
                    role="system",
                    content="Return one JSON decision. Tool execution is unavailable in this test.",
                ),
                Message(role="user", content=objective),
            ),
        )
        raw = await budget.call(
            lambda request=request: provider.generate(request),
            settings.llm_timeout_seconds,
            ErrorCode.LLM_TIMEOUT,
        )
        budget.check()
        decision = parse_decision(raw)
        if expected_type == "tool_call":
            if not isinstance(decision, ToolCallDecision):
                raise HarnessError(ErrorCode.INVALID_DECISION, "Expected a tool proposal in smoke.")
            call = registry.prepare(decision.tool, decision.arguments)
            if call.name != "get_service_status" or call.arguments.model_dump() != {
                "service_name": "checkout-api"
            }:
                raise HarnessError(ErrorCode.INVALID_DECISION, "Unexpected smoke tool proposal.")
        elif not isinstance(decision, FinalDecision):
            raise HarnessError(ErrorCode.INVALID_DECISION, "Expected a final decision in smoke.")
        verified.append(expected_type)
    budget.check()
    schema_json = json.dumps(gemini_decision_schema(registry.descriptions()), sort_keys=True)
    return {
        "status": "passed",
        "checked_at": datetime.now(UTC).isoformat(),
        "model": settings.gemini_model,
        "sdk_version": genai.__version__,
        "schema_sha256": hashlib.sha256(schema_json.encode()).hexdigest(),
        "validated_decisions": verified,
        "llm_requests": len(verified),
        "tool_dispatches": 0,
        "active_runtime_seconds": budget.elapsed,
    }


async def run_smoke(data_dir: Path) -> dict:
    settings = load_settings()
    if settings.llm_provider != "gemini":
        raise ConfigurationError()
    bundle = build_mock_tools(data_dir)
    async with GeminiLLMProvider(settings, bundle.registry.descriptions()) as provider:
        return await check_schema_smoke(provider, bundle.registry, settings)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=Path("mock_data"))
    args = parser.parse_args()
    try:
        result = asyncio.run(run_smoke(args.data_dir))
    except HarnessError as exc:
        print(json.dumps({"status": "failed", "error": exc.info.model_dump(mode="json")}))
        raise SystemExit(1) from None
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
