"""Configuration errors are caught before execution and do not reveal credentials."""

import pytest
from pydantic import SecretStr, ValidationError

from agent_harness.config import Settings, load_settings
from agent_harness.errors import ConfigurationError, ErrorCode


def test_fake_defaults_need_no_credentials() -> None:
    settings = load_settings()
    assert settings.llm_provider == "fake"
    assert settings.gemini_api_key is None
    assert settings.max_agent_steps == 10


def test_dotenv_and_environment_precedence(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / ".env").write_text(
        "LLM_PROVIDER=fake\nMAX_AGENT_STEPS=7\nGEMINI_API_KEY=\nGEMINI_MODEL=\n",
        encoding="utf-8",
    )
    assert load_settings().max_agent_steps == 7
    monkeypatch.setenv("MAX_AGENT_STEPS", "3")
    monkeypatch.setenv("DEFAULT_TOOL_TIMEOUT_SECONDS", "0.25")
    settings = load_settings()
    assert settings.max_agent_steps == 3
    assert settings.default_tool_timeout_seconds == 0.25
    assert settings.gemini_api_key is None


@pytest.mark.parametrize(
    "overrides",
    [
        {},
        {"gemini_api_key": "test-only-placeholder"},
        {"gemini_model": "test-model"},
        {"gemini_model": "  ", "gemini_api_key": "test-only-placeholder"},
        {"gemini_model": "test-model", "gemini_api_key": SecretStr(" ")},
    ],
)
def test_gemini_requires_both_key_and_model(overrides: dict) -> None:
    with pytest.raises(ValidationError, match="Gemini requires"):
        Settings(llm_provider="gemini", **overrides)


def test_live_settings_keep_secrets_out_of_repr_and_serialization() -> None:
    secret = "test-only-placeholder-not-a-real-key"
    settings = Settings(llm_provider="gemini", gemini_api_key=secret, gemini_model="  test-model  ")
    assert settings.gemini_api_key.get_secret_value() == secret
    assert settings.gemini_model == "test-model"
    assert secret not in repr(settings)
    assert "gemini_api_key" not in settings.model_dump()
    assert secret not in settings.model_dump_json()


@pytest.mark.parametrize(
    "overrides",
    [
        {"llm_provider": "unknown"},
        {"max_agent_steps": 0},
        {"max_agent_steps": 2.5},
        {"max_agent_steps": True},
        {"max_active_runtime_seconds": -1},
        {"max_active_runtime_seconds": float("inf")},
        {"llm_timeout_seconds": float("nan")},
        {"default_tool_timeout_seconds": 0},
        {"max_llm_repair_attempts": 2},
        {"max_read_tool_retries": 3},
        {"max_incident_retries": 2},
        {"max_incident_retries": -1},
        {"log_level": "TRACE"},
    ],
)
def test_invalid_limits_and_options_fail_validation(overrides: dict) -> None:
    with pytest.raises(ValidationError):
        Settings(**overrides)


def test_retries_can_be_disabled_and_timeouts_need_not_fit_global_budget() -> None:
    settings = Settings(
        max_llm_repair_attempts=0,
        max_read_tool_retries=0,
        max_incident_retries=0,
        max_active_runtime_seconds=1,
        llm_timeout_seconds=20,
    )
    # Runtime code will clamp operations to remaining time in M2.
    assert settings.llm_timeout_seconds > settings.max_active_runtime_seconds


def test_startup_error_is_typed_and_does_not_expose_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    secret = "test-only-placeholder-not-a-real-key"
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", secret)
    with pytest.raises(ConfigurationError) as caught:
        load_settings()
    assert caught.value.info.code == ErrorCode.INVALID_CONFIGURATION
    assert secret not in str(caught.value)
    assert secret not in caught.value.info.model_dump_json()
