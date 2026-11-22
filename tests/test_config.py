from app.core.config import Settings


def test_settings_defaults() -> None:
    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    assert settings.environment == "development"
    assert settings.database_url.startswith("postgresql+psycopg://")
