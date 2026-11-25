"""Model loading for the deployed container (step 21).

    python -m app.warmup --download   # Docker build: put all models into MODEL_CACHE_DIR
    python -m app.warmup              # load them and time it (what the API does at startup)

The API calls `warm_up()` at startup when WARM_UP_MODELS is true (set in the Dockerfile), so
the models load before uvicorn accepts traffic. With scale-to-zero, the first request after
an idle spell waits for that container start instead of hitting a half-loaded process.
Locally and in tests WARM_UP_MODELS is false and models load lazily as before.
"""

import argparse
import logging
import time

from app.config import get_settings

log = logging.getLogger("app.warmup")


def warm_up() -> dict[str, float]:
    """Load (and download if missing) every model the request path uses; seconds per model."""
    from app.embeddings import embed_query
    from app.retrieval.rerank import score_pairs

    timings: dict[str, float] = {}
    started = time.perf_counter()
    embed_query("casual loading")
    timings["embeddings"] = time.perf_counter() - started

    started = time.perf_counter()
    score_pairs("casual loading", ["Casual employees", "Annual leave"])
    timings["reranker"] = time.perf_counter() - started

    if get_settings().injection_classifier:
        from app.guardrails.classifier import injection_score

        started = time.perf_counter()
        injection_score("What is the casual loading?")
        timings["injection_classifier"] = time.perf_counter() - started
    return {k: round(v, 1) for k, v in timings.items()}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--download", action="store_true", help="download only (Docker build)")
    args = ap.parse_args()
    if args.download:
        from app.guardrails.classifier import ensure_model

        ensure_model()  # always baked in, even if the classifier is switched off later
    print(warm_up())


if __name__ == "__main__":
    main()
