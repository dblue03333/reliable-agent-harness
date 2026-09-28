"""API entry point; execution endpoints will be added with the harness."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import FastAPI
from pydantic import BaseModel

from agent_harness.config import Settings, load_settings


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"


def health() -> HealthResponse:
    """Confirm that the API process can serve requests."""
    return HealthResponse()


def create_app(settings: Settings | None = None) -> FastAPI:
    """Validate configuration at startup, not import time; inject settings in tests."""

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        application.state.settings = settings if settings is not None else load_settings()
        yield

    application = FastAPI(
        title="Reliable Agent Harness",
        description="Operations assistant harness — contracts and settings milestone.",
        version="0.1.0",
        lifespan=lifespan,
    )
    application.get("/health", response_model=HealthResponse, tags=["health"])(health)
    return application


app = create_app()
