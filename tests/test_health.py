"""Smoke test for application startup and request routing."""

from pathlib import Path

import httpx
import pytest

from agent_harness.api import create_app
from agent_harness.config import Settings
from agent_harness.errors import ConfigurationError


async def test_health_endpoint() -> None:
    app = create_app(
        Settings(_env_file=None, mock_data_dir=Path(__file__).resolve().parents[1] / "mock_data")
    )
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert app.state.settings.llm_provider == "fake"


async def test_invalid_live_configuration_prevents_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    app = create_app()
    with pytest.raises(ConfigurationError):
        async with app.router.lifespan_context(app):
            pytest.fail("Application must not start with missing live credentials.")
