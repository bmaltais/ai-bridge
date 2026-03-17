from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    host: str = "127.0.0.1"
    port: int = 8080
    headless: bool = False

    # Response detection
    poll_interval_ms: int = 200
    stable_threshold_ms: int = 1500
    response_timeout_s: int = 120

    # OpenRouter API key (https://openrouter.ai/keys)
    openrouter_api_key: str = ""


settings = Settings()
