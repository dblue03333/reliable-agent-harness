"""Single-process execution API. POST awaits a bounded segment; no background jobs."""

import logging
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Literal

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, computed_field

from agent_harness.config import Settings, load_settings
from agent_harness.demo import scenario_responses
from agent_harness.errors import ErrorCode, ErrorInfo, HarnessError
from agent_harness.harness import AgentHarness
from agent_harness.llm.fake import ScriptedLLMProvider
from agent_harness.llm.gemini import GeminiLLMProvider
from agent_harness.models import (
    CreateExecutionRequest,
    ExecutionEvent,
    ExecutionState,
    IncidentAction,
)
from agent_harness.tools.factory import build_mock_tools

logger = logging.getLogger("agent_harness.api")


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"


class APIError(BaseModel):
    error: ErrorInfo


class ExecutionResponse(ExecutionState):
    @computed_field
    @property
    def pending_action(self) -> IncidentAction | None:
        return next((a for a in self.actions if a.action_id == self.pending_action_id), None)


def response(state: ExecutionState) -> ExecutionResponse:
    return ExecutionResponse.model_validate(state.model_dump())


def error_response(status: int, code: ErrorCode, message: str) -> JSONResponse:
    payload = APIError(error=ErrorInfo(code=code, message=message))
    return JSONResponse(status_code=status, content=payload.model_dump(mode="json"))


async def require_empty_body(request: Request) -> None:
    # Even {} or null is a replacement body: approval accepts only the saved IDs.
    if await request.body():
        raise HarnessError(ErrorCode.INVALID_REQUEST, "Approval decisions require an empty body.")


def create_app(settings: Settings | None = None, *, harness: AgentHarness | None = None) -> FastAPI:
    """One owned runtime per lifespan. An injected harness is owned by the test/caller."""

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        configured = (
            settings
            if settings is not None
            else (harness.settings if harness is not None else load_settings())
        )
        application.state.settings = configured
        logging.basicConfig(level=configured.log_level, format="%(message)s")
        async with AsyncExitStack() as resources:
            if harness is not None:
                runtime = harness
            else:
                bundle = build_mock_tools(configured.mock_data_dir)
                if configured.llm_provider == "gemini":
                    provider = await resources.enter_async_context(
                        GeminiLLMProvider(configured, bundle.registry.descriptions())
                    )
                else:
                    script = scenario_responses("investigation")
                    if configured.fake_scenario == "approval":
                        script = script[:-1] + scenario_responses("approval")
                    provider = ScriptedLLMProvider(script)
                runtime = AgentHarness(provider, bundle.registry, configured)
            application.state.harness = runtime
            try:
                yield
            finally:
                del application.state.harness

    application = FastAPI(
        title="Reliable Agent Harness",
        description=(
            "Local operations assistant with validated tools, bounded execution and explicit "
            "incident approval. POST waits for a stopping point. Fake mode uses a fixed script. "
            "One process/worker; no authentication or durable storage."
        ),
        version="0.1.0",
        lifespan=lifespan,
    )

    @application.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, exc: RequestValidationError):
        # FastAPI's default detail can echo raw payload/validation inputs.
        return error_response(422, ErrorCode.INVALID_REQUEST, "Request does not match the schema.")

    @application.exception_handler(HarnessError)
    async def domain_error(request: Request, exc: HarnessError):
        status = {
            ErrorCode.NOT_FOUND: 404,
            ErrorCode.ACTION_CONFLICT: 409,
            ErrorCode.INVALID_REQUEST: 422,
        }.get(exc.info.code, 500)
        if status == 500:
            logger.error("Unexpected API domain failure; code=%s", exc.info.code.value)
            return error_response(500, ErrorCode.INTERNAL_ERROR, "Unexpected API failure.")
        return error_response(status, exc.info.code, exc.info.message)

    @application.middleware("http")
    async def unexpected_error_boundary(request: Request, call_next):
        try:
            return await call_next(request)
        except Exception as exc:
            # Starlette's catch-all exception handler re-raises to the ASGI server,
            # which can log a raw traceback. Handle before that boundary instead.
            # CancelledError is a BaseException and still propagates to the harness.
            logger.error("Unexpected API failure; type=%s", type(exc).__name__)
            return error_response(500, ErrorCode.INTERNAL_ERROR, "Unexpected API failure.")

    errors = {code: {"model": APIError} for code in (404, 409, 422, 500)}

    @application.get("/health", response_model=HealthResponse, tags=["health"])
    async def health() -> HealthResponse:
        return HealthResponse()

    @application.post(
        "/executions",
        response_model=ExecutionResponse,
        status_code=201,
        responses=errors,
        tags=["executions"],
    )
    async def create_execution(payload: CreateExecutionRequest, request: Request):
        return response(await request.app.state.harness.execute(payload.objective))

    @application.get(
        "/executions/{execution_id}",
        response_model=ExecutionResponse,
        responses=errors,
        tags=["executions"],
    )
    async def get_execution(execution_id: str, request: Request):
        runtime = request.app.state.harness
        async with runtime.store.lock(execution_id):
            return response(runtime.store.get(execution_id))

    @application.get(
        "/executions/{execution_id}/events",
        response_model=list[ExecutionEvent],
        responses=errors,
        tags=["executions"],
    )
    async def get_events(execution_id: str, request: Request):
        runtime = request.app.state.harness
        async with runtime.store.lock(execution_id):
            return runtime.store.events(execution_id)

    @application.post(
        "/executions/{execution_id}/actions/{action_id}/approve",
        response_model=ExecutionResponse,
        responses=errors,
        tags=["approval"],
        description="Approve the exact saved action. Send no request body, including {} or null.",
    )
    async def approve(execution_id: str, action_id: str, request: Request):
        await require_empty_body(request)
        return response(await request.app.state.harness.approve(execution_id, action_id))

    @application.post(
        "/executions/{execution_id}/actions/{action_id}/reject",
        response_model=ExecutionResponse,
        responses=errors,
        tags=["approval"],
        description="Reject the saved action and resume. Send no request body.",
    )
    async def reject(execution_id: str, action_id: str, request: Request):
        await require_empty_body(request)
        return response(await request.app.state.harness.reject(execution_id, action_id))

    return application


app = create_app()
