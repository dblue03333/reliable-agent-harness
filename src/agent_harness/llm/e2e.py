"""Opt-in live verification through the HTTP app and real harness, with mock effects only."""

import argparse
import asyncio
import hashlib
import json
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import httpx
from google import genai

from agent_harness.api import create_app
from agent_harness.config import Settings, load_settings
from agent_harness.errors import ConfigurationError, HarnessError
from agent_harness.harness import AgentHarness
from agent_harness.llm.base import LLMProvider, LLMRequest
from agent_harness.llm.gemini import GeminiLLMProvider
from agent_harness.llm.schema import gemini_decision_schema
from agent_harness.models import IncidentAction
from agent_harness.tools.factory import MockTools, build_mock_tools

Scenario = Literal["read", "approve", "reject"]
OBJECTIVES = {
    "read": (
        "Investigate checkout-api using its service status and relevant knowledge-base runbooks. "
        "Use both read tools, then summarize only observed facts and possible next steps. "
        "Do not propose or create an incident."
    ),
    "approve": (
        "Investigate checkout-api using service status and relevant knowledge-base runbooks. "
        "After reading both, propose exactly one incident about the observed degradation for "
        "human approval. Do not claim it exists before a tool receipt. After approval, give a "
        "final summary including the exact incident_id from the tool receipt. If rejected, "
        "give a final answer acknowledging rejection; do not propose another incident."
    ),
}
OBJECTIVES["reject"] = OBJECTIVES["approve"]


class VerificationFailure(Exception):
    """Only static check names belong here, never raw provider/HTTP output."""


class ObservedProvider:
    """Keep requests in memory to verify observation delivery; never export their content."""

    def __init__(self, delegate: LLMProvider):
        self.delegate = delegate
        self.requests: list[LLMRequest] = []

    async def generate(self, request: LLMRequest) -> str:
        self.requests.append(request)
        return await self.delegate.generate(request)


def require(condition: bool, check: str) -> None:
    if not condition:
        raise VerificationFailure(check)


def observations(request: LLMRequest) -> list[dict]:
    return [json.loads(m.content) for m in request.messages if m.role == "tool"]


async def verify_flow(
    provider: LLMProvider,
    bundle: MockTools,
    settings: Settings,
    scenario: Scenario,
    *,
    approval: Callable[[IncidentAction], bool] | None = None,
) -> dict:
    """One scenario with an isolated harness. Caller owns provider lifetime.

    HTTP is exercised in-process through ASGITransport; Gemini requests in run_e2e
    use real network I/O. This does not claim to test a deployed network/proxy.
    """
    observer = ObservedProvider(provider)
    harness = AgentHarness(observer, bundle.registry, settings)
    app = create_app(harness=harness)
    initial_count = bundle.incidents.incident_count
    state = None
    checks = []
    phase = "create"
    report = {"scenario": scenario, "status": "failed", "checks": checks}
    try:
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://verification"
            ) as client:
                reply = await client.post("/executions", json={"objective": OBJECTIVES[scenario]})
                require(reply.status_code == 201, "creation_http_status")
                state = reply.json()
                execution_id = state["execution_id"]
                require(
                    bundle.incidents.incident_count == initial_count, "zero_effects_before_approval"
                )
                require(
                    not any(r["tool"] == "create_incident" for r in state["tool_history"]),
                    "zero_incident_attempts_before_approval",
                )
                checks.append("zero_effects_before_approval")
                reads = {r["tool"] for r in state["tool_history"] if r["outcome"] == "succeeded"}
                require(
                    {"get_service_status", "search_knowledge_base"} <= reads, "both_reads_succeeded"
                )
                checks.append("both_reads_succeeded")
                require(
                    any(
                        r["tool"] == "get_service_status"
                        and r["outcome"] == "succeeded"
                        and r["result"]["service_name"] == "checkout-api"
                        for r in state["tool_history"]
                    ),
                    "target_service_observed",
                )
                checks.append("target_service_observed")
                if scenario == "read":
                    require(
                        state["status"] == "completed" and not state["actions"],
                        "read_completed_without_action",
                    )
                else:
                    require(state["status"] == "waiting_approval", "approval_pause")
                    require(len(state["actions"]) == 1, "one_pending_action")
                    saved = state["pending_action"]
                    require(saved is not None and saved["status"] == "pending", "pending_snapshot")
                    require(saved["action_id"] == state["pending_action_id"], "pending_identity")
                    checks.append("approval_pause")
                    phase = "human_decision"
                    if scenario == "approve":
                        require(approval is not None, "explicit_mock_approval_required")
                        require(
                            approval(IncidentAction.model_validate_json(json.dumps(saved))),
                            "mock_approval_not_granted",
                        )
                    action_id = saved["action_id"]
                    route = f"/executions/{execution_id}/actions/{action_id}/{scenario}"
                    phase = "resume"
                    reply = await client.post(route)
                    require(reply.status_code == 200, "decision_http_status")
                    state = reply.json()
                    require(len(state["actions"]) == 1, "no_reproposal")
                    resolved = state["actions"][0]
                    require(
                        resolved["action_id"] == action_id
                        and resolved["arguments"] == saved["arguments"],
                        "exact_saved_action",
                    )
                    if scenario == "approve":
                        require(resolved["outcome"] == "succeeded", "validated_incident_receipt")
                        require(
                            bundle.incidents.incident_count == initial_count + 1,
                            "one_mock_incident",
                        )
                        report["incident_id"] = resolved["result"]["incident_id"]
                        require(
                            report["incident_id"] in (state["final_answer"] or ""),
                            "final_cites_receipt",
                        )
                        checks.extend(
                            ["exact_saved_action", "one_mock_incident", "final_cites_receipt"]
                        )
                    else:
                        require(
                            resolved["status"] == "rejected" and resolved["result"] is None,
                            "rejection_without_receipt",
                        )
                        require(
                            bundle.incidents.incident_count == initial_count,
                            "rejection_zero_effects",
                        )
                        require(
                            not any(r["tool"] == "create_incident" for r in state["tool_history"]),
                            "rejection_zero_attempts",
                        )
                        checks.append("rejection_zero_effects")
                    require(state["status"] == "completed", "resumed_completion")
                    require(
                        (await client.post(route)).status_code == 409, "duplicate_decision_conflict"
                    )
                    checks.append("duplicate_decision_conflict")
                phase = "audit"
                require(
                    state["status"] == "completed" and bool(state["final_answer"]), "final_answer"
                )
                require(
                    (await client.get(f"/executions/{execution_id}")).json() == state,
                    "saved_snapshot_consistent",
                )
                events = (await client.get(f"/executions/{execution_id}/events")).json()
                require(
                    [e["sequence"] for e in events] == list(range(1, len(events) + 1)),
                    "ordered_events",
                )
                require(
                    sum(e["event_type"] == "execution_terminated" for e in events) == 1,
                    "one_terminal_event",
                )
                require(len(observer.requests) == state["step_count"], "request_step_accounting")
                require(state["step_count"] <= settings.max_agent_steps, "step_budget")
                require(
                    state["active_runtime_seconds"] <= settings.max_active_runtime_seconds,
                    "runtime_budget",
                )
                # Check that successful observations reached a later model request,
                # not merely that the tools ran somewhere in the process.
                received = [o for request in observer.requests for o in observations(request)]
                for record in state["tool_history"]:
                    if record["outcome"] == "succeeded":
                        require(
                            any(
                                o.get("call_id") == record["call_id"]
                                and o.get("result") == record["result"]
                                for o in received
                            ),
                            "validated_observation_delivered",
                        )
                if scenario == "reject":
                    require(
                        any(
                            o.get("action_id") == state["actions"][0]["action_id"]
                            and o.get("status") == "rejected"
                            for o in received
                        ),
                        "rejection_observation_delivered",
                    )
                checks.extend(
                    [
                        "final_answer",
                        "saved_snapshot_consistent",
                        "ordered_events",
                        "one_terminal_event",
                        "request_step_accounting",
                        "budgets",
                        "validated_observation_delivered",
                    ]
                )
                report["status"] = "passed"
    except VerificationFailure as exc:
        report["failed_check"] = str(exc)
    except HarnessError as exc:
        report["error_code"] = exc.info.code.value
    except Exception:
        # Including HTTP/client/callback errors: do not serialize exception detail.
        report["error_code"] = "verification_error"
    report["phase"] = phase
    report["llm_requests"] = len(observer.requests)
    report["incidents_created"] = bundle.incidents.incident_count - initial_count
    if state is not None:
        latest = harness.store.get(state["execution_id"])
        report.update(
            {
                "execution_id": latest.execution_id,
                "execution_status": latest.status.value,
                "termination_reason": latest.termination_reason,
                "execution_error_code": latest.error.code.value if latest.error else None,
                "steps": latest.step_count,
                "repairs": latest.repair_attempt_count,
                "active_runtime_seconds": latest.active_runtime_seconds,
                "attempts": [
                    {
                        "tool": r.tool,
                        "attempt": r.attempt,
                        "outcome": r.outcome.value,
                        "error_code": r.error.code.value if r.error else None,
                    }
                    for r in latest.tool_history
                ],
                "actions": [
                    {"status": a.status.value, "outcome": a.outcome.value if a.outcome else None}
                    for a in latest.actions
                ],
                "event_types": [
                    e.event_type.value for e in harness.store.events(latest.execution_id)
                ],
            }
        )
    return report


async def run_e2e(
    data_dir: Path,
    scenario: Scenario,
    *,
    approve_mock_incident: bool = False,
    approval: Callable[[IncidentAction], bool] | None = None,
) -> dict:
    settings = load_settings()
    if settings.llm_provider != "gemini":
        raise ConfigurationError()
    # Fixed mock factory: this approval flag cannot dispatch a production incident adapter.
    bundle = build_mock_tools(data_dir)
    digest = hashlib.sha256(
        json.dumps(
            gemini_decision_schema(bundle.registry.descriptions()),
            sort_keys=True,
        ).encode()
    ).hexdigest()
    async with GeminiLLMProvider(settings, bundle.registry.descriptions()) as provider:
        result = await verify_flow(
            provider,
            bundle,
            settings,
            scenario,
            approval=(lambda action: True) if approve_mock_incident else approval,
        )
    return {
        "checked_at": datetime.now(UTC).isoformat(),
        "model": settings.gemini_model,
        "sdk_version": genai.__version__,
        "schema_sha256": digest,
        "transport": "in_process_http_app_real_gemini_network",
        "side_effect_backend": "isolated_in_memory_mock",
        "limits": {
            "steps": settings.max_agent_steps,
            "active_seconds": settings.max_active_runtime_seconds,
        },
        **result,
    }


def confirm_mock(action: IncidentAction) -> bool:
    print(
        json.dumps(
            {"action_id": action.action_id, "arguments": action.arguments.model_dump()}, indent=2
        ),
        file=sys.stderr,
    )
    print("Approve this mock incident? Type approve: ", file=sys.stderr, flush=True)
    try:
        return input().strip().lower() == "approve"
    except EOFError:
        return False


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=Path("mock_data"))
    parser.add_argument("--scenario", choices=("read", "approve", "reject"), required=True)
    parser.add_argument(
        "--approve-mock-incident",
        action="store_true",
        help="Explicitly authorize one synthetic incident for this verification run",
    )
    args = parser.parse_args()
    if args.approve_mock_incident and args.scenario != "approve":
        parser.error("--approve-mock-incident applies only to --scenario approve")
    try:
        result = asyncio.run(
            run_e2e(
                args.data_dir,
                args.scenario,
                approve_mock_incident=args.approve_mock_incident,
                approval=confirm_mock,
            )
        )
    except HarnessError as exc:
        result = {"status": "failed", "error_code": exc.info.code.value}
    except Exception:
        result = {"status": "failed", "error_code": "verification_error"}
    print(json.dumps(result, indent=2))
    raise SystemExit(0 if result["status"] == "passed" else 1)


if __name__ == "__main__":
    main()
