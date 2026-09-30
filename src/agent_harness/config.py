"""Startup settings. Environment text is parsed; tool/LLM contracts stay strict."""

from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import Field, SecretStr, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from agent_harness.errors import ConfigurationError


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
    )

    llm_provider: Literal["fake", "gemini"] = "fake"
    mock_data_dir: Path = Path("mock_data")
    fake_scenario: Literal["investigation", "approval"] = "approval"
    gemini_api_key: SecretStr | None = Field(default=None, exclude=True, repr=False)
    gemini_model: str | None = None
    max_agent_steps: Annotated[int, Field(gt=0)] = 10
    max_active_runtime_seconds: Annotated[float, Field(gt=0)] = 60.0
    llm_timeout_seconds: Annotated[float, Field(gt=0)] = 20.0
    default_tool_timeout_seconds: Annotated[float, Field(gt=0)] = 5.0
    max_llm_repair_attempts: Annotated[int, Field(ge=0, le=1)] = 1
    max_read_tool_retries: Annotated[int, Field(ge=0, le=2)] = 2
    max_incident_retries: Annotated[int, Field(ge=0, le=1)] = 1
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"

    @field_validator("gemini_api_key", "gemini_model", mode="before")
    @classmethod
    def normalize_optional_values(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip() or None
        return value

    @field_validator(
        "max_agent_steps",
        "max_active_runtime_seconds",
        "llm_timeout_seconds",
        "default_tool_timeout_seconds",
        "max_llm_repair_attempts",
        "max_read_tool_retries",
        "max_incident_retries",
        mode="before",
    )
    @classmethod
    def reject_boolean_limits(cls, value: object) -> object:
        if isinstance(value, bool):
            raise ValueError("Limits must be numeric, not boolean.")
        return value

    @model_validator(mode="after")
    def require_live_configuration(self) -> Self:
        if self.llm_provider == "gemini":
            key = self.gemini_api_key
            if key is None or not key.get_secret_value().strip() or not self.gemini_model:
                raise ValueError("Gemini requires GEMINI_API_KEY and GEMINI_MODEL.")
        return self


def load_settings() -> Settings:
    """Load .env and environment (environment wins), without leaking validation inputs."""
    try:
        return Settings()
    except ValidationError:
        raise ConfigurationError() from None
