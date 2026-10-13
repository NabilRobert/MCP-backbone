from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="APP_", env_file=".env")

    database_url: str
    target_database_url: str
    query_timeout_seconds: int = 5
    explain_timeout_seconds: int = 2
    cost_threshold: int = 100_000
    demo_rate_limit: int = 5
    demo_rate_limit_window_seconds: int = 3600


settings = Settings()  # pyright: ignore[reportCallIssue] -- fields load from the environment
