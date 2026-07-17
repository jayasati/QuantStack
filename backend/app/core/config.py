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
    # SQLAlchemy's own defaults (pool_size=5, max_overflow=10 -> 15 total)
    # turned out too small once any single caller fans out widely: e.g.
    # MarketStateReportEngine.generate() alone concurrently touches 12
    # intelligence sub-engines, each doing its own reads/writes, so even
    # ONE such call can briefly want more than 15 connections. Found live
    # (2026-07-14): CandidateGenerationEngine.generate() running that
    # report-generation fan-out concurrently for every candidate compounded
    # this into real connection-pool queuing. Raised as a config knob
    # (not hardcoded) so it can be tuned without a code change as usage grows.
    #
    # pool_size raised 20 -> 40 (perf-audit-2026-07-14 finding 18): only
    # the pool_size-bounded connections are kept open and reused across
    # checkouts -- anything landing in the max_overflow tier gets torn down
    # (SCRAM-reauthenticated from scratch next time) the moment it's
    # checked back in. Live burst concurrency measured at ~36-48
    # simultaneous checkouts against pool_size=20, so most of every burst
    # was hitting that non-reused overflow tier and paying a fresh
    # SCRAM handshake per request. max_overflow left as a genuine ceiling
    # above steady-state burst, not the primary capacity.
    database_pool_size: int = 40
    database_max_overflow: int = 20
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
    # Expanded 2026-07-15 from the original 3 indices to a 25-symbol basket
    # (indices + one-or-two most-liquid F&O names per sector tracked by
    # SectorSource/broker_sectors.py) -- sized to the infra headroom
    # verified live that day (Angel One's 3 req/sec rate limit, the new
    # NSE/BSE fallback sources), not to the full ~180-stock F&O universe.
    # BAJFINANCE, BHARTIARTL and ADANIENT have no matching sector index in
    # SECTOR_TOKENS (NBFC/Telecom aren't tracked sectors) -- relative.py
    # handles that gracefully (sector reference just stays empty), but
    # extend SECTOR_TOKENS/feature_stock_sectors together if that gap is
    # ever worth closing. TATAMOTORS no longer resolves -- confirmed via a
    # live NSE trace (2026-07-15) that Tata Motors demerged into TMPV (Tata
    # Motors Passenger Vehicles Ltd), which is what's listed on Angel One's
    # scrip master now; TMPV replaces it as the 2nd Auto name.
    watchlist: list[str] = Field(
        default_factory=lambda: [
            "NIFTY", "BANKNIFTY", "SENSEX",
            "HDFCBANK", "ICICIBANK", "RELIANCE", "INFY", "TCS", "SBIN",
            "AXISBANK", "LT", "TATASTEEL", "JSWSTEEL", "SUNPHARMA",
            "HINDUNILVR", "ITC", "MARUTI", "TMPV", "DLF", "COALINDIA",
            "ULTRACEMCO", "HCLTECH", "BAJFINANCE", "BHARTIARTL", "ADANIENT",
        ]
    )
    # SmartAPI WebSocket streaming for live quotes (REST polling remains the fallback).
    enable_websocket: bool = False
    # Per-collector schedule overrides, e.g. {"news_intelligence": 300}.
    collector_intervals: dict[str, int] = Field(default_factory=dict)

    # Feature engineering (Volume 3).
    feature_windows: list[int] = Field(default_factory=lambda: [5, 10, 20, 50, 100, 200])
    # "5m" added 2026-07-17 (I-1/intraday-heavy work): this project's actual
    # goal is same-day F&O trading, which a D-only feature layer was never
    # serving (I-1, INVARIANTS.md, VIOLATED since 2026-07-15). First attempt
    # this same day overloaded the scheduled sweep on a 4-vCPU box (25
    # watchlist symbols x 7-8 affected engines x 2 timeframes couldn't
    # complete within one feature_engine_interval cycle -- "maximum number
    # of running instances reached" repeating, /prediction/candidates
    # degraded from 5.7-6.5s to 494-613s) and was reverted, then
    # re-attempted here after resizing quantstack-vm e2-standard-4 ->
    # e2-standard-8 (4 -> 8 vCPU) specifically to give this CPU-bound
    # workload the headroom it needed -- memory was never the constraint
    # (12GB+ free throughout). See DEBT-15 for the full incident record and
    # the live re-verification after the resize.
    feature_timeframes: list[str] = Field(default_factory=lambda: ["D", "5m"])
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
    # Sector names must match broker_sectors.py's SECTOR_TOKENS keys exactly.
    # HDFCBANK was "Banking" until 2026-07-15 -- SECTOR_TOKENS tracks
    # "Banking" and "Private Bank" as distinct indices, and HDFCBANK is a
    # private bank, so it now maps to the more accurate one.
    feature_stock_sectors: dict[str, str] = Field(
        default_factory=lambda: {
            "RELIANCE": "Energy",
            "HDFCBANK": "Private Bank",
            "ICICIBANK": "Private Bank",
            "AXISBANK": "Private Bank",
            "INFY": "IT",
            "TCS": "IT",
            "HCLTECH": "IT",
            "SBIN": "PSU Bank",
            "LT": "Infrastructure",
            "ULTRACEMCO": "Infrastructure",
            "TATASTEEL": "Metal",
            "JSWSTEEL": "Metal",
            "SUNPHARMA": "Pharma",
            "HINDUNILVR": "FMCG",
            "ITC": "FMCG",
            "MARUTI": "Auto",
            "TMPV": "Auto",
            "DLF": "Realty",
            "COALINDIA": "PSU",
        }
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
    # Market State Report regeneration interval, seconds (Volume 4, Prompt
    # 4.15). Must run at least as often as feature_engine_interval since
    # scheduled candidate generation reads the persisted report, not a live
    # compute -- a stale/missing report silently zeroes out market_confidence.
    market_intelligence_interval: int = 300
    # Feature selection sweep interval, seconds (Prompt 3.16, DEBT-9).
    # Matches feature_health_interval's cadence deliberately: selection runs
    # against timeframe="D" bars, which only update once/day at midnight
    # (DEBT-1) -- anything faster than a few hours re-ranks against
    # unchanged data for pure CPU cost.
    feature_selection_interval: int = 21600
    # Ensemble training/prediction sweep interval, seconds (Prompt 5.6,
    # DEBT-13). Same cadence reasoning as feature_selection_interval:
    # training fits against timeframe="D" labels/features that only change
    # once/day, so anything faster just re-trains against unchanged data
    # for real CPU cost (~3.5s/symbol measured live 2026-07-17).
    ensemble_training_interval: int = 21600
    # Number of OS processes serving this app (e.g. `uvicorn --workers N` /
    # gunicorn worker count). MUST be kept truthful by whoever changes the
    # deploy command -- OpportunityLifecycleManager's in-process asyncio.Lock
    # (IRR Critical #2) only provides exclusion within a single process, so a
    # value other than 1 is asserted against at startup rather than silently
    # trusted (see prediction/lifecycle.py's startup guard).
    deployment_workers: int = 1
    feature_expiry_weekday: int = 1
    feature_budget_window_days: int = 5
    feature_market_holidays: list[str] = Field(
        default_factory=lambda: [
            "2026-01-26", "2026-03-04", "2026-04-03", "2026-04-14",
            "2026-05-01", "2026-08-15", "2026-10-02", "2026-11-09",
            "2026-11-10", "2026-12-25",
        ]
    )

    # Git commit deployed, for model/dataset registry provenance (data
    # foundation audit 2026-07-17, model registry item). No `.git` directory
    # is copied into the backend image (Dockerfile.backend intentionally
    # keeps it lean and non-root), so this can't be read at runtime the way
    # a local `git rev-parse HEAD` would -- it's baked in at build time via
    # the GIT_COMMIT Docker build arg (docker-compose.yml), set from the
    # deploying host's own `git rev-parse HEAD` (VERIFY-COOKBOOK.md §10).
    # "unknown" here is the honest default for local dev / any environment
    # that didn't set the build arg, matching this codebase's graceful-
    # degradation convention rather than raising.
    git_commit: str = "unknown"

    # Secrets — no defaults; provided via environment or .env only.
    angel_one_api_key: str | None = None
    angel_one_client_id: str | None = None
    angel_one_pin: str | None = None
    angel_one_totp_secret: str | None = None
    telegram_token: str | None = None
    telegram_chat_id: str | None = None
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
