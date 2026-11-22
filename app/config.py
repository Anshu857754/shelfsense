"""App settings, loaded from environment variables or a local .env file."""

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    # Matches OPENROUTER_API_KEY / OPENROUTER_MODEL in .env (case-insensitive).
    openrouter_api_key: str = ""
    # The model is configuration, not code: swap it in .env without editing Python.
    openrouter_model: str = ""

    # Where the SQLite database and the saved shelf photos live.
    data_dir: str = "data"
    # A detection the model is less sure of than this is kept apart as "uncertain".
    confidence_threshold: float = Field(default=0.5, ge=0, le=1)
    # Photos are shrunk to this many pixels on the long side before going to the model.
    max_image_side: int = Field(default=1600, ge=400, le=4000)
    # Take a zoomed-in second look at uncertain detections before giving up on them.
    verify_uncertain: bool = True
    # How much the AI is asked to report: "full", "lean" or "minimal". Less detail is faster.
    detail_level: str = "full"
    # How many times the AI looks at each photo (in parallel). With more than one, the middle
    # result is used and disagreement lowers the trust level. Costs this many times as much.
    detect_passes: int = Field(default=1, ge=1, le=5)
    # Protection for a public deployment. If set, every page asks for this password (any
    # username works), so strangers who find the address cannot run audits on your AI credit.
    access_password: str = ""
    # Stop after this many AI runs per day (0 = no limit). Another guard on your credit.
    max_audits_per_day: int = Field(default=0, ge=0)
    log_level: str = "INFO"

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @property
    def data_path(self) -> Path:
        path = Path(self.data_dir)
        return path if path.is_absolute() else PROJECT_ROOT / path


@lru_cache
def get_settings() -> Settings:
    # Cached so .env is read once. Never print or log this object: it holds the key.
    return Settings()
