"""Keep local credentials and .env files out of deterministic tests."""

import os

import pytest

from agent_harness.config import Settings


@pytest.fixture(autouse=True)
def isolate_settings(monkeypatch: pytest.MonkeyPatch, tmp_path, request):
    if request.node.get_closest_marker("live") and os.getenv("RUN_LIVE_TESTS") == "1":
        return
    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)


def pytest_collection_modifyitems(items):
    if os.getenv("RUN_LIVE_TESTS") == "1":
        return
    skip = pytest.mark.skip(reason="Live Gemini calls require RUN_LIVE_TESTS=1 and local config.")
    for item in items:
        if item.get_closest_marker("live"):
            item.add_marker(skip)
