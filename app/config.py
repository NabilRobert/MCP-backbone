from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """The server's own configuration, read from environment variables
    (or a local .env file) that start with APP_. The .env file also
    holds unrelated variables for scripts/run_benchmark.py
    (BENCHMARK_MODEL, ANTHROPIC_API_KEY, etc.) -- extra="ignore" tells
    this class to skip over anything it doesn't recognize instead of
    erroring, since this file is shared by more than one script."""

    model_config = SettingsConfigDict(env_prefix="APP_", env_file=".env", extra="ignore")

    database_url: str
    target_database_url: str
    query_timeout_seconds: int = 5
    explain_timeout_seconds: int = 2
    cost_threshold: int = 100_000
    demo_rate_limit: int = 5
    demo_rate_limit_window_seconds: int = 3600


settings = Settings()  # pyright: ignore[reportCallIssue] -- fields load from the environment
