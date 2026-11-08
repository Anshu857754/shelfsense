"""App settings, loaded from environment variables or a local .env file."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # Matches OPENROUTER_API_KEY / OPENROUTER_MODEL in .env (case-insensitive).
    openrouter_api_key: str = ""
    # The model is configuration, not code: swap it in .env without editing Python.
    openrouter_model: str = ""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


@lru_cache
def get_settings() -> Settings:
    # Cached so .env is read once. Never print or log this object: it holds the key.
    return Settings()
