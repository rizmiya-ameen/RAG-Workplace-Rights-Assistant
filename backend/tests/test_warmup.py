"""Step 21: the deployed API loads its models at startup (WARM_UP_MODELS), not mid-request."""

from fastapi.testclient import TestClient

from app import main, warmup
from app.config import get_settings


def test_startup_warms_up_models_when_enabled(monkeypatch):
    calls = []
    monkeypatch.setattr(get_settings(), "warm_up_models", True)
    monkeypatch.setattr(warmup, "warm_up", lambda: calls.append(1) or {"embeddings": 0.1})
    with TestClient(main.app) as c:  # the context manager runs the lifespan
        assert calls == [1]
        assert c.get("/health").json() == {"status": "ok"}


def test_no_warm_up_by_default(monkeypatch):
    calls = []
    monkeypatch.setattr(warmup, "warm_up", lambda: calls.append(1) or {})
    assert get_settings().warm_up_models is False
    with TestClient(main.app):
        pass
    assert calls == []


def test_warm_up_loads_each_model(monkeypatch):
    from app import embeddings
    from app.guardrails import classifier
    from app.retrieval import rerank

    loaded = []
    monkeypatch.setattr(embeddings, "embed_query", lambda q: loaded.append("embeddings"))
    monkeypatch.setattr(rerank, "score_pairs", lambda q, p: loaded.append("reranker"))
    monkeypatch.setattr(classifier, "injection_score", lambda q: loaded.append("classifier"))
    monkeypatch.setattr(get_settings(), "injection_classifier", True)
    timings = warmup.warm_up()
    assert loaded == ["embeddings", "reranker", "classifier"]
    assert set(timings) == {"embeddings", "reranker", "injection_classifier"}
