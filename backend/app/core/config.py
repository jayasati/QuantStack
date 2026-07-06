"""Configuration system.

Resolution priority (highest first):
1. Environment variables
2. `.env` file (repo root)
3. `configs/default.yaml`

Never hardcode values in application code — everything is configurable.
"""

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    YamlConfigSettingsSource,
)

# Repo root: backend/app/core/config.py -> parents[3]
REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_YAML = REPO_ROOT / "configs" / "default.yaml"


class RateLimits(BaseSettings):
    angel_one_per_second: int = 3
    telegram_per_minute: int = 20


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=REPO_ROOT / ".env",
        env_file_encoding="utf-8",
        yaml_file=DEFAULT_YAML if DEFAULT_YAML.exists() else None,
        extra="ignore",
    )

    app_name: str = "QuantStack"
    environment: str = "development"
    database_url: str = "postgresql+asyncpg://quantstack:quantstack@localhost:5432/quantstack"
    redis_url: str = "redis://localhost:6379/0"
    log_level: str = "INFO"
    max_retry: int = 3
    cache_timeout: int = 300
    rate_limits: RateLimits = Field(default_factory=RateLimits)
    watchlist: list[str] = Field(default_factory=lambda: ["NIFTY", "BANKNIFTY"])
    # SmartAPI WebSocket streaming for live quotes (REST polling remains the fallback).
    enable_websocket: bool = False
    # Per-collector schedule overrides, e.g. {"news_intelligence": 300}.
    collector_intervals: dict[str, int] = Field(default_factory=dict)

    # Feature engineering (Volume 3).
    feature_windows: list[int] = Field(default_factory=lambda: [5, 10, 20, 50, 100, 200])
    feature_timeframes: list[str] = Field(default_factory=lambda: ["D"])
    feature_benchmark_symbol: str = "NIFTY"
    feature_engine_interval: int = 300
    # Candles loaded per run; must exceed the largest rolling window.
    feature_candle_lookback: int = 500

    # Secrets — no defaults; provided via environment or .env only.
    angel_one_api_key: str | None = None
    angel_one_client_id: str | None = None
    angel_one_pin: str | None = None
    angel_one_totp_secret: str | None = None
    telegram_token: str | None = None
    openai_key: str | None = None

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # Order = priority: env > .env > default.yaml
        return (
            init_settings,
            env_settings,
            dotenv_settings,
            YamlConfigSettingsSource(settings_cls),
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
