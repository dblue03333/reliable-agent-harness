"""HTTP contracts through an app lifespan and real harness; isolated per test."""

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

from agent_harness.api import create_app
from agent_harness.config import Settings
from agent_harness.demo import scenario_responses
from agent_harness.errors import ErrorCode, HarnessError
from agent_harness.harness import AgentHarness
from agent_harness.llm.fake import ScriptedLLMProvider
from agent_harness.models import ExecutionStatus
from agent_harness.tools.factory import build_mock_tools

DATA = Path(__file__).resolve().parents[1] / "mock_data"
FINAL = '{"type":"final","answer":"Review recorded results."}'
PROPOSAL = scenario_responses("approval")[0]


@asynccontextmanager
async def client_for(app):
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://test",
        ) as client:
            yield client


def custom(script=(PROPOSAL, FINAL), **settings):
    bundle = build_mock_tools(DATA)
    harness = AgentHarness(ScriptedLLMProvider(script), bundle.registry, Settings(**settings))
    return create_app(harness=harness), harness, bundle


async def pending(client):
    result = await client.post("/executions", json={"objective": "Inspect checkout"})
    assert result.status_code == 201
    body = result.json()
    assert body["status"] == "waiting_approval"
    assert body["pending_action"]["arguments"]["severity"] == "high"
    return body, f"/executions/{body['execution_id']}/actions/{body['pending_action_id']}"


@pytest.mark.parametrize("choice", ["approve", "reject"])
async def test_default_app_shared_runtime_across_requests_and_ordered_events(choice):
    app = create_app(Settings(mock_data_dir=DATA))
    async with client_for(app) as client:
        body, route = await pending(client)
        execution = body["execution_id"]
        assert len(body["tool_history"]) == 2
        assert all(r["tool"] != "create_incident" for r in body["tool_history"])
        result = await client.post(f"{route}/{choice}")
        assert result.status_code == 200
        state = result.json()
        assert state["status"] == "completed"
        assert state["pending_action"] is None
        assert state["actions"][0]["status"] == ("resolved" if choice == "approve" else "rejected")
        assert (await client.get(f"/executions/{execution}")).json() == state
        assert (await client.post(f"{route}/{choice}")).status_code == 409
        events = (await client.get(f"/executions/{execution}/events")).json()
        assert [e["sequence"] for e in events] == list(range(1, len(events) + 1))
        assert sum(e["event_type"] == "execution_terminated" for e in events) == 1
        assert events[-1]["status"] == "completed"
        assert sum(r["tool"] == "create_incident" for r in state["tool_history"]) == int(
            choice == "approve"
        )
    assert not hasattr(app.state, "harness")


async def test_investigation_mode_and_restart_have_isolated_stores():
    app = create_app(Settings(mock_data_dir=DATA, fake_scenario="investigation"))
    async with client_for(app) as client:
        result = await client.post("/executions", json={"objective": "Inspect"})
        state = result.json()
        assert state["status"] == "completed"
        assert state["actions"] == []
    async with client_for(app) as client:
        assert (await client.get("/executions/" + state["execution_id"])).status_code == 404


@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", "/executions/missing"),
        ("GET", "/executions/missing/events"),
        ("POST", "/executions/missing/actions/missing/approve"),
        ("POST", "/executions/missing/actions/missing/reject"),
    ],
)
async def test_not_found_http_mapping(method, path):
    app, _, _ = custom()
    async with client_for(app) as client:
        result = await client.request(method, path)
        assert result.status_code == 404
        assert result.json()["error"]["code"] == "not_found"


@pytest.mark.parametrize("choice", ["approve", "reject"])
async def test_foreign_action_has_404_and_keeps_both_pending(choice):
    app, _, bundle = custom()
    async with client_for(app) as client:
        first, _ = await pending(client)
        second, _ = await pending(client)
        path = f"/executions/{first['execution_id']}/actions/{second['pending_action_id']}/{choice}"
        assert (await client.post(path)).status_code == 404
        for state in (first, second):
            result = await client.get("/executions/" + state["execution_id"])
            assert result.json()["status"] == "waiting_approval"
        assert bundle.incidents.incident_count == 0


@pytest.mark.parametrize("body", ["{}", "null", " ", '{"severity":"critical"}', "broken"])
@pytest.mark.parametrize("choice", ["approve", "reject"])
async def test_decision_body_rejected_without_claim(body, choice):
    app, harness, bundle = custom()
    async with client_for(app) as client:
        state, route = await pending(client)
        result = await client.post(f"{route}/{choice}", content=body)
        assert result.status_code == 422
        assert harness.store.get(state["execution_id"]).status == ExecutionStatus.WAITING_APPROVAL
        assert bundle.incidents.incident_count == 0


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"objective": " "},
        {"objective": 1},
        {"objective": "x" * 4001},
        {"objective": "secret-marker", "extra": "secret-marker"},
        None,
    ],
)
async def test_bad_payload_is_safe_422(payload):
    app, _, _ = custom()
    async with client_for(app) as client:
        result = await client.post("/executions", json=payload)
        assert result.status_code == 422
        assert result.json()["error"]["code"] == "invalid_request"
        assert "secret-marker" not in result.text


async def test_malformed_json_is_safe_422():
    app, _, _ = custom()
    async with client_for(app) as client:
        result = await client.post(
            "/executions", content='{"secret-marker":', headers={"Content-Type": "application/json"}
        )
        assert result.status_code == 422
        assert "secret-marker" not in result.text


@pytest.mark.parametrize(
    "script,settings,reason",
    [
        (("{}", "{}"), {}, "invalid_decision"),
        ((PROPOSAL, FINAL), {"max_agent_steps": 1}, "step_limit"),
    ],
)
async def test_domain_failure_is_snapshot_not_http_error(script, settings, reason):
    app, _, bundle = custom(script, **settings)
    async with client_for(app) as client:
        result = await client.post("/executions", json={"objective": "Inspect"})
        assert result.status_code == 201
        state = result.json()
        if state["pending_action_id"]:
            result = await client.post(
                f"/executions/{state['execution_id']}/actions/{state['pending_action_id']}/approve"
            )
            assert result.status_code == 200
            state = result.json()
            assert state["actions"][0]["result"]["incident_id"]
            assert bundle.incidents.incident_count == 1
        assert state["error"]["code"] == reason
        assert (await client.get("/executions/" + state["execution_id"])).json() == state


async def test_receipt_readable_after_provider_summary_failure():
    app, harness, bundle = custom()
    harness.provider.generate = AsyncMock(
        side_effect=[PROPOSAL, RuntimeError("secret SDK failure")]
    )
    async with client_for(app) as client:
        initial, route = await pending(client)
        result = await client.post(route + "/approve")
        assert result.status_code == 200
        state = result.json()
        assert state["status"] == "failed"
        assert state["actions"][0]["outcome"] == "succeeded"
        assert bundle.incidents.incident_count == 1
        assert "secret SDK" not in result.text
        assert (await client.get("/executions/" + initial["execution_id"])).json() == state


async def test_http_race_one_claim_and_reads_do_not_wait_for_tool_io():
    app, harness, bundle = custom()
    entered, release = asyncio.Event(), asyncio.Event()
    original = bundle.registry.execute_claimed_incident

    async def slow(action):
        entered.set()
        await release.wait()
        return await original(action)

    bundle.registry.execute_claimed_incident = slow
    async with client_for(app) as client:
        state, route = await pending(client)
        task = asyncio.create_task(client.post(route + "/approve"))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            current = await asyncio.wait_for(client.get("/executions/" + state["execution_id"]), 1)
            assert current.json()["status"] == "running"
            assert (await client.post(route + "/reject")).status_code == 409
            assert (await client.post(route + "/approve")).status_code == 409
        finally:
            release.set()
        assert (await task).json()["status"] == "completed"
        assert bundle.incidents.incident_count == 1


async def test_cancelled_request_cleans_execution_and_preserves_unknown():
    app, harness, bundle = custom()
    entered = asyncio.Event()

    async def slow(action):
        entered.set()
        await asyncio.Event().wait()

    bundle.registry.execute_claimed_incident = slow
    async with client_for(app) as client:
        state, route = await pending(client)
        task = asyncio.create_task(client.post(route + "/approve"))
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        current = (await client.get("/executions/" + state["execution_id"])).json()
        assert current["termination_reason"] == "cancelled"
        assert current["actions"][0]["outcome"] == "outcome_unknown"


@pytest.mark.parametrize(
    "failure", [RuntimeError("secret-api"), HarnessError(ErrorCode.LLM_ERROR, "secret-api")]
)
async def test_unexpected_api_error_has_sanitized_500(failure, caplog):
    app, harness, _ = custom()
    harness.execute = AsyncMock(side_effect=failure)
    async with client_for(app) as client:
        result = await client.post("/executions", json={"objective": "Inspect"})
        assert result.status_code == 500
        assert result.json()["error"]["code"] == "internal_error"
        assert "secret-api" not in result.text + caplog.text


async def test_owned_gemini_client_created_once_and_closed_on_lifespan_exit(monkeypatch):
    provider = AsyncMock()
    provider.__aenter__.return_value = provider
    provider.generate.return_value = FINAL
    calls = []

    def factory(*args):
        calls.append(args)
        return provider

    monkeypatch.setattr("agent_harness.api.GeminiLLMProvider", factory)
    app = create_app(
        Settings(
            mock_data_dir=DATA, llm_provider="gemini", gemini_api_key="dummy", gemini_model="test"
        )
    )
    async with client_for(app) as client:
        for _ in range(2):
            assert (await client.post("/executions", json={"objective": "Inspect"})).json()[
                "status"
            ] == "completed"
        assert len(calls) == 1
        provider.__aexit__.assert_not_awaited()
    provider.__aexit__.assert_awaited_once()


async def test_openapi_describes_routes_models_and_bodyless_approval():
    app, _, _ = custom()
    schema = app.openapi()
    assert len(schema["paths"]) == 6
    assert schema["paths"]["/executions"]["post"]["responses"]["201"]
    for choice in ("approve", "reject"):
        operation = schema["paths"]["/executions/{execution_id}/actions/{action_id}/" + choice][
            "post"
        ]
        assert "requestBody" not in operation
        assert all(str(code) in operation["responses"] for code in (200, 404, 409, 422, 500))


async def test_handled_internal_error_does_not_escape_to_asgi_server():
    app, harness, _ = custom()
    harness.execute = AsyncMock(side_effect=RuntimeError("secret traceback payload"))
    async with app.router.lifespan_context(app):
        # A server must not receive/re-log the raw exception after sending sanitized JSON.
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, raise_app_exceptions=True),
            base_url="http://test",
        ) as client:
            result = await client.post("/executions", json={"objective": "Inspect"})
            assert result.status_code == 500
            assert "secret traceback" not in result.text


async def test_apps_and_concurrent_executions_do_not_share_state():
    first = create_app(Settings(mock_data_dir=DATA))
    second = create_app(Settings(mock_data_dir=DATA))
    async with client_for(first) as client, client_for(second) as other:
        responses = await asyncio.gather(
            *[
                client.post("/executions", json={"objective": objective})
                for objective in ("First", "Second")
            ]
        )
        a, b = (r.json() for r in responses)
        assert a["execution_id"] != b["execution_id"]
        assert a["pending_action_id"] != b["pending_action_id"]
        assert (a["objective"], b["objective"]) == ("First", "Second")
        assert a["step_count"] == b["step_count"] == 3
        assert (await other.get("/executions/" + a["execution_id"])).status_code == 404


async def test_missing_dataset_prevents_startup_before_constructing_provider(monkeypatch, tmp_path):
    factory = AsyncMock()
    monkeypatch.setattr("agent_harness.api.GeminiLLMProvider", factory)
    app = create_app(
        Settings(
            mock_data_dir=tmp_path / "missing",
            llm_provider="gemini",
            gemini_api_key="dummy",
            gemini_model="test",
        )
    )
    with pytest.raises(HarnessError) as caught:
        async with app.router.lifespan_context(app):
            pytest.fail("Bad dataset must not start the app")
    assert caught.value.info.code == ErrorCode.INVALID_MOCK_DATA
    factory.assert_not_called()
    assert not hasattr(app.state, "harness")


async def test_owned_provider_closed_even_when_application_lifespan_fails(monkeypatch):
    provider = AsyncMock()
    provider.__aenter__.return_value = provider
    monkeypatch.setattr("agent_harness.api.GeminiLLMProvider", lambda *args: provider)
    app = create_app(
        Settings(
            mock_data_dir=DATA,
            llm_provider="gemini",
            gemini_api_key="dummy",
            gemini_model="test",
        )
    )
    with pytest.raises(RuntimeError):
        async with app.router.lifespan_context(app):
            raise RuntimeError("Test failure")
    provider.__aexit__.assert_awaited_once()
    assert not hasattr(app.state, "harness")


@pytest.mark.parametrize("fault", ["output", "timeout", "provider"])
async def test_tool_and_provider_failures_are_readable_http_snapshots(fault):
    from agent_harness.tools.registry import ToolRegistry, ToolSpec
    from agent_harness.tools.schemas import SearchKnowledgeBaseInput, SearchKnowledgeBaseOutput

    read = '{"type":"tool_call","tool":"search_knowledge_base","arguments":{"query":"x"}}'
    handler = AsyncMock(return_value={"matches": "bad"})
    if fault == "timeout":
        handler.side_effect = TimeoutError()
    registry = ToolRegistry(
        [
            ToolSpec(
                name="search_knowledge_base",
                description="Fault wrapper",
                input_schema=SearchKnowledgeBaseInput,
                output_schema=SearchKnowledgeBaseOutput,
                handler=handler,
            )
        ]
    )
    provider = ScriptedLLMProvider((read, FINAL))
    if fault == "provider":
        provider = AsyncMock()
        provider.generate.side_effect = RuntimeError("secret upstream failure")
    harness = AgentHarness(provider, registry, Settings(max_read_tool_retries=0))
    async with client_for(create_app(harness=harness)) as client:
        result = await client.post("/executions", json={"objective": "Inspect"})
        assert result.status_code == 201
        state = result.json()
        assert state["status"] == "failed"
        assert (
            state["error"]["code"]
            == {
                "output": "invalid_tool_output",
                "timeout": "tool_timeout",
                "provider": "llm_error",
            }[fault]
        )
        assert "secret upstream" not in result.text
        saved = await client.get("/executions/" + state["execution_id"])
        assert saved.json() == state
        events = (await client.get("/executions/" + state["execution_id"] + "/events")).json()
        assert events[-1]["event_type"] == "execution_terminated"


@pytest.mark.parametrize(
    "method,path,status,code",
    [
        ("GET", "/unknown", 404, "not_found"),
        ("PUT", "/executions", 405, "invalid_request"),
        ("POST", "/health", 405, "invalid_request"),
    ],
)
async def test_framework_routing_errors_use_api_envelope(method, path, status, code):
    app, _, _ = custom()
    async with client_for(app) as client:
        result = await client.request(method, path)
        assert result.status_code == status
        assert result.json()["error"]["code"] == code
        assert "detail" not in result.json()
        if status == 405:
            assert result.headers["allow"]


@pytest.mark.parametrize("status", [400, 401, 429, 503])
async def test_framework_exception_details_are_sanitized_and_headers_preserved(status):
    from starlette.exceptions import HTTPException

    app, _, _ = custom()

    @app.get("/test-http-error")
    async def fail():
        raise HTTPException(status, detail="secret diagnostic", headers={"Retry-After": "10"})

    async with client_for(app) as client:
        result = await client.get("/test-http-error")
        assert result.status_code == status
        assert result.json()["error"]["code"] == (
            "internal_error" if status >= 500 else "invalid_request"
        )
        assert "secret diagnostic" not in result.text
        assert result.headers["retry-after"] == "10"
