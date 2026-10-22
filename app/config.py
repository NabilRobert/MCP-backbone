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
    # Reject a query estimated pricier than this. Calibrated against
    # real Pagila data -- see .env.example and JOURNEY.md for the
    # actual measurements behind this number and a known gap it
    # doesn't close.
    cost_threshold: int = 40_000
    demo_rate_limit: int = 5  # max run_query calls per workspace per window
    demo_rate_limit_window_seconds: int = 3600  # how long that window is
    # sample_rows returns real data too, so it's audited and limited as
    # well -- but on its own, larger allowance, since it's how a client
    # learns what the data looks like before spending a run_query.
    sample_rate_limit: int = 30
    # Encrypts every stored target-database connection URL (they hold
    # passwords). A Fernet key: generate one with
    #   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    # Keep a copy somewhere safe off the server -- lose it and every
    # stored connection URL becomes unreadable.
    connection_encryption_key: str = ""
    # Lets a workspace's target database resolve to a loopback/private
    # address. Off by default: once users can register their own
    # connection URL, one pointing at 127.0.0.1 would reach this
    # server's own bookkeeping database. Only for local development and
    # the test suite, where the target genuinely is localhost.
    allow_private_targets: bool = False
    # Console login: lock out a client IP after this many failed
    # attempts within the window. Also applied to failed signups.
    login_max_failures: int = 5
    login_failure_window_seconds: int = 900
    # Signs the web console's login session cookie. If left unset, a
    # random one is generated at startup -- fine for local use, but it
    # means everyone gets logged out whenever the server restarts.
    # Set this explicitly for anything longer-lived.
    console_session_secret: str = Field(default_factory=lambda: secrets.token_urlsafe(32))
    # Marks the console's session cookie Secure, so a browser only ever
    # sends it over HTTPS. Turn on for any real deployment; it has to
    # stay off for local development over plain http://, or the browser
    # drops the cookie and login silently never sticks.
    console_cookie_https_only: bool = False
    # The MCP server's own DNS-rebinding protection only allows the Host
    # header to be 127.0.0.1/localhost unless told otherwise -- real,
    # not theoretical: a real deploy got every external request rejected
    # with "421 Misdirected Request" until this was added. Comma-
    # separated hostnames (no port, no scheme) this server is actually
    # reachable at, e.g. "mcp.example.com" for a real deployment.
    mcp_allowed_hosts: str = ""

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
