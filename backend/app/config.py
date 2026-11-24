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

    # OpenAI-compatible LLM endpoint (build step 10). Default: Google Gemini free tier
    # (key from aistudio.google.com in backend/.env). Ollama offline:
    # http://127.0.0.1:11434/v1 with qwen2.5:3b.
    llm_base_url: str = "https://generativelanguage.googleapis.com/v1beta/openai/"
    llm_api_key: str = ""
    llm_model: str = "gemini-3.5-flash-lite"
    llm_timeout_s: float = 60.0
    # Thinking models spend output tokens on hidden reasoning; "low" keeps answers fast and
    # stops the answer being cut off. Empty = don't send the parameter (e.g. for Ollama).
    llm_reasoning_effort: str = "low"
    llm_max_tokens: int = 2048
    # Free tiers return 503 (busy) and 429 (rate limit) at times: retry with backoff.
    llm_max_retries: int = 2

    # Retrieval for /chat: "vector" won the step 9 eval (hit@6 0.90 vs 0.80 hybrid).
    retrieval_mode: str = "vector"
    retrieval_k: int = 6


@lru_cache
def get_settings() -> Settings:
    return Settings()
