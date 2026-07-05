from app.core.config import Settings


def test_settings_defaults_load() -> None:
    settings = Settings()
    assert settings.app_name == "QuantStack"
    assert settings.database_url.startswith("postgresql+asyncpg://")
    assert settings.max_retry >= 1
    assert settings.rate_limits.angel_one_per_second >= 1


def test_env_overrides_defaults(monkeypatch) -> None:
    monkeypatch.setenv("APP_NAME", "OverriddenName")
    settings = Settings()
    assert settings.app_name == "OverriddenName"
