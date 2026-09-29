"""Exercise the real SDK against an in-memory HTTP transport; no external requests."""

import asyncio
import json
from pathlib import Path

import httpx
import pytest
from google import genai

from agent_harness.config import Settings
from agent_harness.errors import ConfigurationError, ErrorCode, HarnessError
from agent_harness.harness import AgentHarness
from agent_harness.llm.base import LLMRequest
from agent_harness.llm.gemini import GeminiLLMProvider
from agent_harness.llm.schema import gemini_decision_schema
from agent_harness.llm.smoke import check_schema_smoke
from agent_harness.models import ExecutionStatus, Message
from agent_harness.tools.factory import build_mock_tools

FINAL = '{"type":"final","answer":"Done"}'
TOOL = (
    '{"type":"tool_call","tool":"get_service_status","arguments":{"service_name":"checkout-api"}}'
)


@pytest.fixture
def registry():
    return build_mock_tools(Path(__file__).resolve().parents[1] / "mock_data").registry


@pytest.fixture
def settings():
    return Settings(
        llm_provider="gemini",
        gemini_api_key="unit-test-key-never-sent-to-network",
        gemini_model="unit-test-model",
        llm_timeout_seconds=2,
    )


def response_body(text=FINAL, *, reason="STOP", parts=None):
    if text in (FINAL, TOOL):
        text = json.dumps({"decision": json.loads(text)})
    return {
        "candidates": [
            {
                "content": {
                    "role": "model",
                    "parts": parts if parts is not None else [{"text": text}],
                },
                "finishReason": reason,
            }
        ]
    }


@pytest.fixture
def wire(monkeypatch):
    """Replace only HTTP transport; retain SDK conversion/retry/response handling."""
    original_client = genai.Client
    clients, requests, options = [], [], []

    def install(handler):
        async def transport(request):
            requests.append(request)
            return await handler(request)

        def factory(**kwargs):
            options.append(kwargs)
            kwargs["http_options"] = kwargs["http_options"].model_copy(
                update={"async_client_args": {"transport": httpx.MockTransport(transport)}}
            )
            client = original_client(**kwargs)
            # Observe the SDK-owned transport client so cleanup matches production;
            # externally supplied httpx clients remain the caller's responsibility.
            clients.append(client._api_client._async_httpx_client)
            return client

        monkeypatch.setattr("agent_harness.llm.gemini.genai.Client", factory)
        return requests, options, clients

    return install


async def test_sdk_wire_schema_roles_no_autocalls_no_retries_and_client_cleanup(
    settings, registry, wire
):
    async def handler(request):
        return httpx.Response(200, json=response_body())

    requests, options, clients = wire(handler)
    request = LLMRequest(
        execution_id="exec_wire",
        objective="Inspect",
        step=2,
        messages=(
            Message(role="system", content="Trusted harness instructions"),
            Message(role="user", content="Inspect checkout"),
            Message(role="assistant", content=TOOL),
            Message(role="tool", content='{"status":"degraded"}'),
        ),
    )
    async with GeminiLLMProvider(settings, registry.descriptions()) as provider:
        raw = await provider.generate(request)
        assert json.loads(raw) == json.loads(FINAL)
    assert all(client.is_closed for client in clients)
    assert len(requests) == 1
    assert options[0]["vertexai"] is False
    assert options[0]["enterprise"] is False
    assert options[0]["http_options"].retry_options.attempts == 1
    assert options[0]["http_options"].timeout == 2000
    body = json.loads(requests[0].content)
    config = body["generationConfig"]
    assert config["responseMimeType"] == "application/json"
    assert config["responseJsonSchema"] == gemini_decision_schema(registry.descriptions())
    assert "tools" not in body
    assert [c["role"] for c in body["contents"]] == ["user", "model", "user"]
    assert "Trusted harness instructions" in body["systemInstruction"]["parts"][0]["text"]
    assert "Untrusted tool observation" in body["contents"][-1]["parts"][0]["text"]
    assert body["contents"][1]["parts"][0]["text"] == TOOL
    assert "unit-test-key" not in str(requests[0].url)
    with pytest.raises(HarnessError):
        await provider.generate(request)
    await provider.aclose()  # Idempotent cleanup.


def test_projection_uses_contracts_and_preserves_incident_title(registry):
    descriptions = registry.descriptions()
    schema = gemini_decision_schema(descriptions)
    assert schema["required"] == ["decision"]
    assert schema["additionalProperties"] is False
    branches = schema["properties"]["decision"]["anyOf"]
    assert len(branches) == 4
    for branch, description in zip(branches[:-1], descriptions, strict=True):
        assert branch["properties"]["type"]["enum"] == ["tool_call"]
        assert branch["properties"]["tool"]["enum"] == [description["name"]]
        args = branch["properties"]["arguments"]
        assert args["additionalProperties"] is False
        assert args["required"] == description["parameters"]["required"]
        assert set(args["properties"]) == set(description["parameters"]["properties"])
    incident = branches[2]["properties"]["arguments"]
    assert incident["properties"]["title"]["maxLength"] == 200
    assert incident["properties"]["severity"]["enum"] == ["low", "medium", "high", "critical"]
    assert branches[-1]["properties"]["answer"]["maxLength"] == 8000
    encoded = json.dumps(schema)
    assert all(keyword not in encoded for keyword in ('"$ref"', '"$defs"', '"discriminator"'))
    assert descriptions == registry.descriptions()
    schema["properties"]["decision"]["anyOf"].clear()
    assert len(gemini_decision_schema(descriptions)["properties"]["decision"]["anyOf"]) == 4


@pytest.mark.parametrize("status_code", [400, 401, 403, 429, 500, 503])
async def test_http_failures_are_sanitized_and_never_retried(settings, registry, wire, status_code):
    async def handler(request):
        return httpx.Response(
            status_code,
            json={"error": {"code": status_code, "message": "secret raw provider diagnostic"}},
        )

    requests, _, clients = wire(handler)
    async with GeminiLLMProvider(settings, registry.descriptions()) as provider:
        with pytest.raises(HarnessError) as exc:
            await provider.generate(LLMRequest(execution_id="exec_1", objective="Inspect", step=1))
    assert exc.value.info.code == ErrorCode.LLM_ERROR
    assert "secret" not in str(exc.value)
    assert len(requests) == 1
    assert all(client.is_closed for client in clients)


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"promptFeedback": {"blockReason": "SAFETY"}},
        response_body(reason="MAX_TOKENS"),
        response_body(reason="SAFETY"),
        response_body(parts=[]),
        response_body(text="  "),
        response_body(parts=[{"text": "private reasoning", "thought": True}]),
        response_body(parts=[{"functionCall": {"name": "create_incident", "args": {}}}]),
        {"candidates": response_body()["candidates"] * 2},
    ],
)
async def test_incomplete_blocked_or_nontext_response_is_not_a_decision(
    settings, registry, wire, body
):
    async def handler(request):
        return httpx.Response(200, json=body)

    requests, _, _ = wire(handler)
    async with GeminiLLMProvider(settings, registry.descriptions()) as provider:
        with pytest.raises(HarnessError) as exc:
            await provider.generate(LLMRequest(execution_id="exec_1", objective="Inspect", step=1))
    assert exc.value.info.code == ErrorCode.LLM_ERROR
    assert len(requests) == 1


async def test_only_public_text_is_returned_and_harness_validates_malformed_json(
    settings, registry, wire
):
    async def handler(request):
        return httpx.Response(
            200,
            json=response_body(
                parts=[{"text": "private reasoning", "thought": True}, {"text": "not JSON"}]
            ),
        )

    requests, _, _ = wire(handler)
    async with GeminiLLMProvider(settings, registry.descriptions()) as provider:
        harness = AgentHarness(provider, registry, settings)
        state = await harness.execute("Inspect")
    assert state.status == ExecutionStatus.FAILED
    assert state.error.code == ErrorCode.INVALID_DECISION
    assert "private reasoning" not in state.model_dump_json()
    assert state.step_count == len(requests) == 1
    assert state.tool_history == ()


async def test_gemini_adapter_runs_through_real_harness_read_loop(settings, registry, wire):
    responses = iter((TOOL, FINAL))

    async def handler(request):
        return httpx.Response(200, json=response_body(next(responses)))

    requests, _, _ = wire(handler)
    async with GeminiLLMProvider(settings, registry.descriptions()) as provider:
        state = await AgentHarness(provider, registry, settings).execute("Investigate checkout")
    assert state.status == ExecutionStatus.COMPLETED
    assert state.step_count == len(requests) == 2
    assert state.tool_history[0].result["status"] == "degraded"
    body = json.loads(requests[-1].content)
    assert "degraded" in body["contents"][-1]["parts"][0]["text"]


@pytest.mark.parametrize("cancel_externally", [False, True])
async def test_timeout_and_external_cancellation_close_resources(
    settings, registry, wire, cancel_externally
):
    entered, cancelled = asyncio.Event(), asyncio.Event()

    async def handler(request):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    requests, _, clients = wire(handler)
    limits = Settings.model_validate(
        settings.model_dump()
        | {
            "gemini_api_key": settings.gemini_api_key,
            "llm_timeout_seconds": 2 if cancel_externally else 0.03,
        }
    )
    async with GeminiLLMProvider(limits, registry.descriptions()) as provider:
        task = asyncio.create_task(
            provider.generate(LLMRequest(execution_id="exec_1", objective="Inspect", step=1))
        )
        await asyncio.wait_for(entered.wait(), timeout=2)
        if cancel_externally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            with pytest.raises(HarnessError) as exc:
                await task
            assert exc.value.info.code == ErrorCode.LLM_TIMEOUT
    assert cancelled.is_set()
    assert len(requests) == 1
    assert all(client.is_closed for client in clients)


async def test_schema_smoke_validates_both_decisions_without_dispatch(
    settings, registry, wire, monkeypatch
):
    responses = iter((TOOL, FINAL))

    async def handler(request):
        return httpx.Response(200, json=response_body(next(responses)))

    async def forbidden(*args, **kwargs):
        pytest.fail("Schema smoke must never execute tools")

    monkeypatch.setattr(registry, "execute", forbidden)
    monkeypatch.setattr(registry, "execute_claimed_incident", forbidden)
    requests, _, _ = wire(handler)
    async with GeminiLLMProvider(settings, registry.descriptions()) as provider:
        result = await check_schema_smoke(provider, registry, settings)
    assert result["validated_decisions"] == ["tool_call", "final"]
    assert result["llm_requests"] == len(requests) == 2
    assert result["tool_dispatches"] == 0
    assert result["model"] == settings.gemini_model


def test_fake_settings_cannot_silently_construct_gemini(registry, monkeypatch):
    def forbidden(**kwargs):
        pytest.fail("No SDK client should be constructed")

    monkeypatch.setattr("agent_harness.llm.gemini.genai.Client", forbidden)
    with pytest.raises(ConfigurationError):
        GeminiLLMProvider(Settings(), registry.descriptions())


@pytest.mark.parametrize(
    "envelope",
    [
        {},
        {"decision": []},
        {"decision": None},
        {"decision": {"type": "final", "answer": "Done"}, "extra": True},
    ],
)
async def test_provider_rejects_missing_or_extra_envelope_fields(
    settings, registry, wire, envelope
):
    async def handler(request):
        return httpx.Response(200, json=response_body(text=json.dumps(envelope)))

    wire(handler)
    async with GeminiLLMProvider(settings, registry.descriptions()) as provider:
        with pytest.raises(HarnessError) as exc:
            await provider.generate(LLMRequest(execution_id="exec_1", objective="Inspect", step=1))
    assert exc.value.info.code == ErrorCode.INVALID_DECISION
