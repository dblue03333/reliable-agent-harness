"""Offline harness CLI: scripted decisions through the real harness and mock registry."""

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

from pydantic import ValidationError

from agent_harness.config import load_settings
from agent_harness.errors import ErrorCode, HarnessError
from agent_harness.harness import AgentHarness
from agent_harness.llm.fake import ScriptedLLMProvider
from agent_harness.models import ExecutionStatus
from agent_harness.tools.factory import build_mock_tools


def scenario_responses(scenario: str) -> tuple[str, ...]:
    status = json.dumps(
        {
            "type": "tool_call",
            "tool": "get_service_status",
            "arguments": {"service_name": "checkout-api"},
        }
    )
    if scenario == "step-limit":
        return (status,)
    if scenario in ("incident-blocked", "incident-pending", "approval"):
        proposal = (
            json.dumps(
                {
                    "type": "tool_call",
                    "tool": "create_incident",
                    "arguments": {
                        "title": "Investigate checkout timeouts",
                        "description": "Demo proposal; human approval has not been granted.",
                        "severity": "high",
                    },
                }
            ),
        )
        if scenario == "approval":
            return proposal + (
                json.dumps(
                    {
                        "type": "final",
                        "answer": "Scripted demo finished. See the stored action outcome.",
                    }
                ),
            )
        return proposal
    return (
        status,
        json.dumps(
            {
                "type": "tool_call",
                "tool": "search_knowledge_base",
                "arguments": {"query": "checkout timeout payment"},
            }
        ),
        json.dumps(
            {
                "type": "final",
                "answer": (
                    "Scripted demo complete: service status and matching runbooks are "
                    "available in tool_history. No incident was created. This fixed script "
                    "does not reason about the objective or diagnose a root cause."
                ),
            }
        ),
    )


async def run_demo(args: argparse.Namespace) -> int:
    settings = load_settings()
    if settings.llm_provider != "fake":
        raise HarnessError(
            ErrorCode.INVALID_CONFIGURATION,
            "This offline CLI requires LLM_PROVIDER=fake; use agent_harness.llm.smoke for Gemini.",
        )
    logging.basicConfig(level=settings.log_level, format="%(message)s")
    bundle = build_mock_tools(args.data_dir)
    harness = AgentHarness(
        ScriptedLLMProvider(
            scenario_responses(args.scenario), repeat_last=args.scenario == "step-limit"
        ),
        bundle.registry,
        settings,
    )
    state = await harness.execute(args.objective)
    if args.scenario == "approval" and state.status == ExecutionStatus.WAITING_APPROVAL:
        action = next(a for a in state.actions if a.action_id == state.pending_action_id)
        # Human waiting happens outside any active segment. No flag pre-approves a
        # payload that the caller has not yet seen. This CLI owns one execution.
        print(
            json.dumps(
                {
                    "execution_id": state.execution_id,
                    "pending_action": action.model_dump(mode="json"),
                },
                indent=2,
            ),
            file=sys.stderr,
        )
        print(
            "Review the exact action above. Type approve or reject (anything else leaves pending):",
            file=sys.stderr,
            flush=True,
        )
        try:
            choice = input().strip().lower()
        except EOFError:
            choice = ""
        if choice == "approve":
            state = await harness.approve(state.execution_id, action.action_id)
        elif choice == "reject":
            state = await harness.reject(state.execution_id, action.action_id)
    print(
        json.dumps(
            {
                "mode": "scripted_fake",
                "scenario": args.scenario,
                "execution": state.model_dump(mode="json"),
                "events": [
                    event.model_dump(mode="json", exclude_none=True)
                    for event in harness.store.events(state.execution_id)
                ],
                "incident_count": bundle.incidents.incident_count,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    if state.status == ExecutionStatus.WAITING_APPROVAL:
        return 3
    return 0 if state.status == ExecutionStatus.COMPLETED else 1


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--objective", required=True, help="Stored user objective; script stays fixed"
    )
    parser.add_argument("--data-dir", type=Path, default=Path("mock_data"))
    parser.add_argument(
        "--scenario",
        choices=("investigation", "step-limit", "incident-pending", "incident-blocked", "approval"),
        default="investigation",
    )
    args = parser.parse_args()
    try:
        code = asyncio.run(run_demo(args))
    except HarnessError as exc:
        print(exc.info.model_dump_json())
        code = 2
    except ValidationError:
        print(json.dumps({"error": "Invalid CLI input; check objective and configuration."}))
        code = 2
    raise SystemExit(code)


if __name__ == "__main__":
    main()
