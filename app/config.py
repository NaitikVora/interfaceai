"""Process configuration, loaded from environment variables and an optional `.env` file.

Every tunable limit lives here so that no module carries magic constants. Secrets are typed as
``SecretStr`` so they never appear in reprs or logs by accident.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

PLACEHOLDER_API_KEY = "sk-replace-with-your-key"
"""The value shipped in `.env.example`; treated as "not configured" so the error is helpful."""


class Settings(BaseSettings):
    """All runtime configuration. Field names map to upper-cased environment variables."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- LLM (discovery only) -------------------------------------------------------------
    llm_api_key: SecretStr | None = None
    llm_model: str = "gpt-4.1"
    llm_base_url: str = "https://api.openai.com/v1"
    llm_timeout_s: float = 90.0
    llm_max_parse_retries: int = Field(default=3, ge=1, le=10)
    llm_temperature: float = 0.0
    llm_max_output_tokens: int = 1200

    # --- Demo application -----------------------------------------------------------------
    demo_app_url: str = "http://localhost:8000"
    demo_app_port: int = 8000
    demo_operator_id: str = "teller1"
    demo_access_code: SecretStr = SecretStr("teller-pass")
    demo_supervisor_id: str = "super1"
    demo_supervisor_code: SecretStr = SecretStr("super-pass")

    # --- Browser ----------------------------------------------------------------------------
    headless: bool = True
    browser_slow_mo_ms: int = 0
    viewport_width: int = 1280
    viewport_height: int = 900

    # --- Discovery loop limits --------------------------------------------------------------
    agent_max_steps: int = Field(default=25, ge=1, le=200)
    agent_max_runtime_s: float = 600.0
    agent_max_history: int = Field(default=12, ge=1)
    agent_stuck_repeats: int = Field(default=3, ge=2)
    agent_max_wait_s: float = 5.0

    # --- Observation bounds (token-conscious) ----------------------------------------------
    obs_max_text_chars: int = 4000
    obs_max_controls: int = 60
    obs_max_tables: int = 6
    obs_max_table_rows: int = 12
    obs_max_table_cols: int = 8
    obs_max_cell_chars: int = 48
    obs_max_extracted_chars: int = 200

    # --- Replay ----------------------------------------------------------------------------
    replay_default_step_timeout_s: float = 10.0
    replay_max_runtime_s: float = 300.0
    replay_poll_interval_s: float = 0.15
    escalation_timeout_s: float = 900.0
    operator_port: int = 8001

    # --- Paths ------------------------------------------------------------------------------
    artifacts_dir: Path = Path("artifacts")
    evidence_dir: Path = Path("evidence")
    policy_file: Path = Path("policies/demobank.json")
    profile_file: Path = Path("profiles/demobank_legacycore.json")

    @property
    def llm_configured(self) -> bool:
        """True when a real LLM call can be attempted (the example placeholder does not count)."""
        if self.llm_api_key is None:
            return False
        value = self.llm_api_key.get_secret_value()
        return bool(value) and value != PLACEHOLDER_API_KEY


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings singleton."""
    return Settings()
