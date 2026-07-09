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
    # Error-handling escalation chain (Chapter 13): consecutive failures
    # before a circuit breaker opens, and how long it stays open before a
    # single half-open probe is allowed through.
    circuit_breaker_failure_threshold: int = 3
    circuit_breaker_recovery_seconds: float = 60.0
    # Collector-level retry around collect() (Chapter 20's retry_count):
    # deliberately small/flat, not max_retry's network-request backoff — many
    # sources already retry internally (AngelOneAdapter, NseSession), so this
    # layer only needs to smooth over a blip they didn't catch, not repeat
    # their whole multi-second backoff on top of itself.
    collector_retry_attempts: int = 1
    collector_retry_delay_seconds: float = 0.2
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
    # Trailing bars used for rolling z-score normalization (_z features).
    feature_normalization_window: int = 100
    # Symbol whose closes provide implied volatility for VIX-distance features.
    feature_vix_symbol: str = "INDIAVIX"
    # Quote snapshots loaded per liquidity run (from market_events).
    feature_quote_lookback: int = 500
    # Reference order size (qty) for the market-impact estimate.
    feature_reference_order_qty: int = 1000
    # Option-chain observations loaded per options-feature run.
    feature_options_lookback: int = 5000
    # Breadth observations loaded per breadth-feature run.
    feature_breadth_lookback: int = 8000
    # Sector observations loaded per sector-feature run.
    feature_sector_lookback: int = 8000
    # Institutional flow observations loaded per flow-feature run.
    feature_flow_lookback: int = 8000
    # Macro observations loaded per macro-feature run.
    feature_macro_lookback: int = 8000
    # Relative-strength references (Prompt 3.8): stock -> sector index name.
    feature_stock_sectors: dict[str, str] = Field(
        default_factory=lambda: {"RELIANCE": "Energy", "HDFCBANK": "Banking", "INFY": "IT"}
    )
    # Stock -> industry index; falls back to the sector when unset (no finer
    # industry index data source yet).
    feature_stock_industries: dict[str, str] = Field(default_factory=dict)
    feature_sensex_symbol: str = "SENSEX"
    # Market structure (Prompt 3.9): fractal pivot half-width and the intraday
    # timeframe used for session features.
    feature_structure_fractal: int = 2
    feature_intraday_timeframe: str = "5m"
    # News articles / calendar observations loaded per feature run.
    feature_news_lookback: int = 5000
    # Sentiment scorer for NewsIntelligenceCollector/GlobalShockCollector:
    # "finbert" (ProsusAI/finbert, real financial-text ML model, ~440MB
    # download + CPU inference) or "lexicon" (dependency-free word-list
    # scoring, useful for offline/CI environments without the model cached).
    news_sentiment_provider: str = "finbert"
    feature_events_lookback: int = 5000
    # Time features (Prompt 3.12): NSE index derivative expiry weekday
    # (0=Mon; Tuesday since Sep 2025), budget window half-width, and the
    # exchange holiday calendar (ISO dates).
    # Feature quality + drift sweep interval, seconds (Prompts 3.14/3.15).
    feature_health_interval: int = 21600
    feature_expiry_weekday: int = 1
    feature_budget_window_days: int = 5
    feature_market_holidays: list[str] = Field(
        default_factory=lambda: [
            "2026-01-26", "2026-03-04", "2026-04-03", "2026-04-14",
            "2026-05-01", "2026-08-15", "2026-10-02", "2026-11-09",
            "2026-11-10", "2026-12-25",
        ]
    )

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
