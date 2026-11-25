# API image (step 21). Built in GitHub Actions (no Docker on the dev laptop), pushed to
# ghcr.io and run on Azure Container Apps.
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.13.8 /uv /uvx /bin/

ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    MODEL_CACHE_DIR=/models \
    HF_HUB_DISABLE_TELEMETRY=1

WORKDIR /app

# Runtime dependencies only (no pytest/ruff). The cache mount keeps uv's download cache out
# of the image.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev

COPY app ./app

# Bake the models into the image (~870 MB: bge-small, the MiniLM re-ranker and the pinned
# injection classifier), so a container scaled up from zero never waits on Hugging Face.
RUN python -m app.warmup --download

RUN useradd --uid 10001 --no-create-home app && chown -R app /models
USER app

# Runtime: no model downloads (they are in /models), load them before accepting traffic.
ENV HF_HUB_OFFLINE=1 \
    WARM_UP_MODELS=true \
    APP_ENV=production

EXPOSE 8000
# --proxy-headers: behind Azure's ingress the client IP comes from X-Forwarded-For (the rate
# limiter keys on it); only the ingress can reach the container, so any forwarder is trusted.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers", "--forwarded-allow-ips", "*", "--no-access-log"]
