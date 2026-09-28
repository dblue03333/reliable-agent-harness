"""API entry point; execution endpoints will be added with the harness."""

from typing import Literal

from fastapi import FastAPI
from pydantic import BaseModel

app = FastAPI(
    title="Reliable Agent Harness",
    description="Operations assistant harness — bootstrap API.",
    version="0.1.0",
)


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"


@app.get("/health", response_model=HealthResponse, tags=["health"])
def health() -> HealthResponse:
    """Confirm that the API process can serve requests."""
    return HealthResponse()
