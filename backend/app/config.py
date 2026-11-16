"""App settings, read from environment variables and backend/.env (if present)."""

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parents[1]
REPO_DIR = BACKEND_DIR.parent


class Settings(BaseSettings):
    # Absolute path, so commands work from any folder.
    model_config = SettingsConfigDict(env_file=BACKEND_DIR / ".env", extra="ignore")

    app_env: str = "dev"

    # Dev: Neon connection string in backend/.env. This default is the optional local Docker
    # Postgres. 127.0.0.1, not localhost: on Windows localhost tries IPv6 (::1) first and hangs.
    database_url: str = "postgresql://wra:wra@127.0.0.1:5432/wra"

    # Repo-level data/ folder (raw pages, sidecars).
    data_dir: Path = REPO_DIR / "data"

    embedding_model: str = "BAAI/bge-small-en-v1.5"  # 384 dims; must match chunks.embedding
    embedding_batch_size: int = 16  # small batches keep memory low on the 7 GB laptop
    # ONNX model files; repo-level .cache/ (git-ignored, on E:, not the full C: drive).
    model_cache_dir: Path = REPO_DIR / ".cache" / "models"

    # OpenAI-compatible LLM endpoint (build step 10). Default: Ollama on this machine.
    llm_base_url: str = "http://127.0.0.1:11434/v1"
    llm_api_key: str = "ollama"
    llm_model: str = "qwen2.5:3b"
    # Local CPU answers take 20-60 s, so keep this generous.
    llm_timeout_s: float = 120.0


@lru_cache
def get_settings() -> Settings:
    return Settings()
