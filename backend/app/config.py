"""App settings, read from environment variables (and a local .env file if present)."""

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: str = "dev"

    # 127.0.0.1, not localhost: on Windows localhost tries IPv6 (::1) first and hangs,
    # because Compose publishes Postgres on 127.0.0.1 only.
    database_url: str = "postgresql://wra:wra@127.0.0.1:5432/wra"

    # Repo-level data/ folder: <repo>/data on the host, /data in the container (mounted).
    data_dir: Path = Path(__file__).resolve().parents[2] / "data"

    # OpenAI-compatible LLM endpoint. Dev: Ollama on the Windows host.
    llm_base_url: str = "http://host.docker.internal:11434/v1"
    llm_api_key: str = "ollama"
    llm_model: str = "qwen2.5:3b"
    # CPU answers take 20-60 s, so keep this generous.
    llm_timeout_s: float = 120.0


@lru_cache
def get_settings() -> Settings:
    return Settings()
