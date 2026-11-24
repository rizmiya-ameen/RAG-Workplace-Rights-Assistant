from app.config import Settings

_LLM_VARS = ("LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL", "LLM_TIMEOUT_S", "LLM_REASONING_EFFORT")


def test_defaults_point_at_gemini(monkeypatch):
    for name in _LLM_VARS:
        monkeypatch.delenv(name, raising=False)
    settings = Settings(_env_file=None)
    assert settings.llm_base_url.startswith("https://generativelanguage.googleapis.com/")
    assert settings.llm_model == "gemini-3.5-flash-lite"
    assert settings.llm_api_key == ""  # the key only ever comes from backend/.env
    assert settings.llm_reasoning_effort == "low"
    assert settings.retrieval_mode == "vector"


def test_env_vars_override_defaults(monkeypatch):
    monkeypatch.setenv("LLM_MODEL", "some-hosted-model")
    monkeypatch.setenv("LLM_TIMEOUT_S", "30")
    monkeypatch.setenv("LLM_REASONING_EFFORT", "")
    settings = Settings(_env_file=None)
    assert settings.llm_model == "some-hosted-model"
    assert settings.llm_timeout_s == 30.0
    assert settings.llm_reasoning_effort == ""
