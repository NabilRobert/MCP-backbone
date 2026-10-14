"""Reads this server's own settings from environment variables (or a
local .env file) into one typed object that the rest of the app imports
and uses -- see .env.example for what every value actually means."""

import secrets

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """The server's own configuration, read from environment variables
    (or a local .env file) that start with APP_. The .env file also
    holds unrelated variables used only by scripts/run_benchmark.py
    (SUMOPOD_API_KEY, AI_MODEL, etc.) -- extra="ignore" tells this class
    to skip over anything it doesn't recognize instead of erroring,
    since this one file is shared by more than one script."""

    model_config = SettingsConfigDict(env_prefix="APP_", env_file=".env", extra="ignore")

    database_url: str  # this project's own bookkeeping database
    target_database_url: str  # the outside database AI clients can query
    query_timeout_seconds: int = 5  # kill a real query after this long
    explain_timeout_seconds: int = 2  # kill the cost-estimate step after this long
    cost_threshold: int = 100_000  # reject a query estimated pricier than this
    demo_rate_limit: int = 5  # max requests per workspace per window
    demo_rate_limit_window_seconds: int = 3600  # how long that window is
    # Signs the web console's login session cookie. If left unset, a
    # random one is generated at startup -- fine for local use, but it
    # means everyone gets logged out whenever the server restarts.
    # Set this explicitly for anything longer-lived.
    console_session_secret: str = Field(default_factory=lambda: secrets.token_urlsafe(32))

    @field_validator("console_session_secret", mode="after")
    @classmethod
    def _fall_back_to_random_secret_if_blank(cls, value: str) -> str:
        # Without this, APP_CONSOLE_SESSION_SECRET= (present in .env but
        # left empty, e.g. straight from .env.example) would set this to
        # an actual empty string instead of falling through to the
        # random default -- confirmed by testing, not assumed. An empty
        # signing key defeats the whole point of signing the cookie.
        return value if value else secrets.token_urlsafe(32)


# Built once, at import time, from whatever's in the environment/.env
# file -- every other file just does "from app.config import settings".
settings = Settings()  # pyright: ignore[reportCallIssue] -- fields load from the environment
