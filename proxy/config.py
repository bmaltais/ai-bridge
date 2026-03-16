from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# Cookies and session data live outside the repo so they are never committed.
# Default: ~/.claude/ai-bridge/cookies/  (overridable via COOKIES_PATH in .env)
_DEFAULT_COOKIES_PATH = Path.home() / ".claude" / "ai-bridge" / "cookies" / "use-ai.json"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    host: str = "127.0.0.1"
    port: int = 8080
    headless: bool = True
    use_ai_url: str = "https://use.ai"
    cookies_path: Path = _DEFAULT_COOKIES_PATH

    # Response detection
    poll_interval_ms: int = 200
    stable_threshold_ms: int = 1500
    response_timeout_s: int = 120


settings = Settings()
