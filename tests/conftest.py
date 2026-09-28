"""Keep local credentials and .env files out of deterministic tests."""

import pytest

from agent_harness.config import Settings


@pytest.fixture(autouse=True)
def isolate_settings(monkeypatch: pytest.MonkeyPatch, tmp_path):
    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)
