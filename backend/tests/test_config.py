from app.config import Settings


def test_defaults_point_at_local_ollama(monkeypatch):
    for name in ("LLM_BASE_URL", "LLM_MODEL", "LLM_TIMEOUT_S"):
        monkeypatch.delenv(name, raising=False)
    settings = Settings(_env_file=None)
    assert settings.llm_base_url == "http://127.0.0.1:11434/v1"
    assert settings.llm_model == "qwen2.5:3b"
    assert settings.llm_timeout_s >= 60


def test_env_vars_override_defaults(monkeypatch):
    monkeypatch.setenv("LLM_MODEL", "some-hosted-model")
    monkeypatch.setenv("LLM_TIMEOUT_S", "30")
    settings = Settings(_env_file=None)
    assert settings.llm_model == "some-hosted-model"
    assert settings.llm_timeout_s == 30.0
