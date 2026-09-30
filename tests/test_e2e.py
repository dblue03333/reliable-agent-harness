"""Verify the live gate offline, including failures that must never become a pass."""

import asyncio
import json
from pathlib import Path

import httpx
import pytest
from google import genai

from agent_harness.config import Settings
from agent_harness.demo import scenario_responses
from agent_harness.errors import ConfigurationError
from agent_harness.llm import e2e
from agent_harness.llm.fake import ScriptedLLMProvider
from agent_harness.tools.factory import build_mock_tools

DATA = Path(__file__).resolve().parents[1] / "mock_data"
READS = scenario_responses("investigation")[:-1]
PROPOSAL = scenario_responses("approval")[0]
FINAL = json.dumps({"type": "final", "answer": "Receipt INC-1001; private-summary-marker"})


def script(scenario):
    return (*READS, FINAL) if scenario == "read" else (*READS, PROPOSAL, FINAL)


@pytest.mark.parametrize(
    "scenario,steps,incidents", [("read", 3, 0), ("approve", 4, 1), ("reject", 4, 0)]
)
async def test_complete_flow_and_sanitized_evidence(scenario, steps, incidents):
    reviewed = []

    def approve(action):
        reviewed.append(action)
        return True

    result = await e2e.verify_flow(
        ScriptedLLMProvider(script(scenario)),
        build_mock_tools(DATA),
        Settings(),
        scenario,
        approval=approve,
    )
    assert result["status"] == "passed", result
    assert result["steps"] == result["llm_requests"] == steps
    assert result["incidents_created"] == incidents
    assert len(reviewed) == (scenario == "approve")
    assert result["event_types"].count("execution_terminated") == 1
    assert "validated_observation_delivered" in result["checks"]
    serialized = json.dumps(result)
    for private in (
        "private-summary-marker",
        "Demo proposal",
        "checkout-api",
        "messages",
        "arguments",
    ):
        assert private not in serialized


@pytest.mark.parametrize(
    "approval,failed",
    [(None, "explicit_mock_approval_required"), (lambda a: False, "mock_approval_not_granted")],
)
async def test_approval_missing_or_denied_leaves_zero_effects(approval, failed):
    result = await e2e.verify_flow(
        ScriptedLLMProvider(script("approve")),
        build_mock_tools(DATA),
        Settings(),
        "approve",
        approval=approval,
    )
    assert result["status"] == "failed"
    assert result["failed_check"] == failed
    assert result["execution_status"] == "waiting_approval"
    assert result["incidents_created"] == 0
    assert result["llm_requests"] == 3


@pytest.mark.parametrize(
    "responses,settings,failed",
    [
        ((FINAL,), {}, "both_reads_succeeded"),
        ((*READS, FINAL), {}, "approval_pause"),
        (
            (*READS, PROPOSAL, '{"type":"final","answer":"Made up receipt INC-9999"}'),
            {},
            "final_cites_receipt",
        ),
        ((*READS, PROPOSAL, PROPOSAL), {}, "no_reproposal"),
        (script("approve"), {"max_agent_steps": 2}, "approval_pause"),
        (script("approve"), {"max_agent_steps": 3}, "final_cites_receipt"),
        (("malformed-secret", "malformed-secret"), {}, "both_reads_succeeded"),
    ],
)
async def test_incomplete_or_incorrect_flow_fails(responses, settings, failed):
    result = await e2e.verify_flow(
        ScriptedLLMProvider(responses),
        build_mock_tools(DATA),
        Settings(**settings),
        "approve",
        approval=lambda a: True,
    )
    assert result["status"] == "failed"
    assert result["failed_check"] == failed
    assert "malformed-secret" not in json.dumps(result)
    if settings.get("max_agent_steps") == 3:
        assert result["termination_reason"] == "llm_step_budget_exhausted"
        assert result["incidents_created"] == 1
        assert result["incident_id"] == "INC-1001"


async def test_callback_failure_is_sanitized_and_never_dispatches():
    def broken(action):
        raise RuntimeError("private-callback-token")

    result = await e2e.verify_flow(
        ScriptedLLMProvider(script("approve")),
        build_mock_tools(DATA),
        Settings(),
        "approve",
        approval=broken,
    )
    assert result["error_code"] == "verification_error"
    assert result["incidents_created"] == 0
    assert "private-callback-token" not in json.dumps(result)


async def test_provider_failure_after_effect_preserves_receipt():
    result = await e2e.verify_flow(
        ScriptedLLMProvider((*READS, PROPOSAL)),
        build_mock_tools(DATA),
        Settings(),
        "approve",
        approval=lambda a: True,
    )
    assert result["status"] == "failed"
    assert result["execution_error_code"] == "llm_error"
    assert result["incidents_created"] == 1
    assert result["incident_id"] == "INC-1001"
    assert result["actions"] == [{"status": "resolved", "outcome": "succeeded"}]


async def test_cancellation_is_not_swallowed():
    entered = asyncio.Event()

    class Waiting:
        async def generate(self, request):
            entered.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(
        e2e.verify_flow(Waiting(), build_mock_tools(DATA), Settings(), "read")
    )
    await asyncio.wait_for(entered.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=2)


async def test_live_runner_rejects_offline_settings():
    with pytest.raises(ConfigurationError):
        await e2e.run_e2e(DATA, "read")


@pytest.mark.parametrize("scenario", ["read", "approve", "reject"])
async def test_real_sdk_full_flow_over_mock_http_and_owned_client_cleanup(monkeypatch, scenario):
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "private-dummy-key")
    monkeypatch.setenv("GEMINI_MODEL", "offline-model")
    original = genai.Client
    clients, requests = [], []
    responses = iter(script(scenario))

    async def transport(request):
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "content": {
                            "role": "model",
                            "parts": [
                                {"text": json.dumps({"decision": json.loads(next(responses))})}
                            ],
                        },
                        "finishReason": "STOP",
                    }
                ]
            },
        )

    def factory(**kwargs):
        kwargs["http_options"] = kwargs["http_options"].model_copy(
            update={"async_client_args": {"transport": httpx.MockTransport(transport)}}
        )
        client = original(**kwargs)
        clients.append(client._api_client._async_httpx_client)
        return client

    monkeypatch.setattr("agent_harness.llm.gemini.genai.Client", factory)
    result = await e2e.run_e2e(DATA, scenario, approve_mock_incident=scenario == "approve")
    assert result["status"] == "passed", result
    assert result["model"] == "offline-model"
    assert len(result["schema_sha256"]) == 64
    assert len(requests) == result["steps"]
    assert all(client.is_closed for client in clients)
    assert "private-dummy-key" not in json.dumps(result)


@pytest.mark.parametrize("status,code", [("passed", 0), ("failed", 1)])
def test_cli_exit_status(monkeypatch, capsys, status, code):
    async def run(*args, **kwargs):
        return {"status": status}

    monkeypatch.setattr(e2e, "run_e2e", run)
    monkeypatch.setattr("sys.argv", ["e2e", "--scenario", "read"])
    with pytest.raises(SystemExit) as exc:
        e2e.main()
    assert exc.value.code == code
    assert json.loads(capsys.readouterr().out)["status"] == status


def test_cli_missing_live_config_fails_safely(monkeypatch, capsys):
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    monkeypatch.setattr("sys.argv", ["e2e", "--scenario", "read"])
    with pytest.raises(SystemExit) as exc:
        e2e.main()
    assert exc.value.code == 1
    assert json.loads(capsys.readouterr().out) == {
        "status": "failed",
        "error_code": "invalid_configuration",
    }


def test_cli_rejects_approval_flag_for_read(monkeypatch):
    monkeypatch.setattr("sys.argv", ["e2e", "--scenario", "read", "--approve-mock-incident"])
    with pytest.raises(SystemExit) as exc:
        e2e.main()
    assert exc.value.code == 2


async def test_read_order_is_not_fixed():
    result = await e2e.verify_flow(
        ScriptedLLMProvider((*reversed(READS), FINAL)),
        build_mock_tools(DATA),
        Settings(),
        "read",
    )
    assert result["status"] == "passed", result


async def test_successful_reads_for_wrong_service_do_not_pass():
    result = await e2e.verify_flow(
        ScriptedLLMProvider((READS[0].replace("checkout-api", "payment-api"), READS[1], FINAL)),
        build_mock_tools(DATA),
        Settings(),
        "read",
    )
    assert result["status"] == "failed"
    assert result["failed_check"] == "target_service_observed"
