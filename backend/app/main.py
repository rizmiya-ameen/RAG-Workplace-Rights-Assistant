import logging
import time
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app import observability as obs
from app.config import get_settings
from app.db.conn import connect
from app.llm import LLMError
from app.logs import client_hash, configure_logging
from app.pipeline import ChatResponse, answer_question
from app.ratelimit import limiter

configure_logging()
# uvicorn's access log prints raw client IPs; the middleware below logs requests instead.
logging.getLogger("uvicorn.access").disabled = True
log = logging.getLogger("app.http")


@asynccontextmanager
async def lifespan(_: FastAPI):
    yield
    obs.flush()  # send buffered traces before the process exits


app = FastAPI(
    title="Workplace Rights Assistant",
    description="General information about Australian Modern Awards, the NES and WorkSafe "
    "Victoria guidance. Not legal advice.",
    version="0.1.0",
    lifespan=lifespan,
)


def setup_cors(target: FastAPI, origins_csv: str) -> list[str]:
    """Allow only the listed browser origins (the deployed web UI). No cookies are used, so
    no credentials; only the methods and header the UI needs."""
    origins = [o.strip() for o in origins_csv.split(",") if o.strip()]
    if origins:
        target.add_middleware(
            CORSMiddleware,
            allow_origins=origins,
            allow_methods=["GET", "POST"],
            allow_headers=["Content-Type"],
            expose_headers=["X-Request-ID", "Retry-After"],
            allow_credentials=False,
            max_age=600,
        )
    return origins


setup_cors(app, get_settings().cors_origins)


@app.middleware("http")
async def access_log(request: Request, call_next):
    """One JSON line per request: method, path, status, time, request_id, hashed client.
    No query strings, bodies or raw IPs."""
    started = time.perf_counter()
    status = 500
    response = None
    try:
        response = await call_next(request)
        status = response.status_code
        return response
    finally:
        log.info(
            "request",
            extra={
                "method": request.method,
                "path": request.url.path,
                "status": status,
                "latency_ms": round(1000 * (time.perf_counter() - started), 1),
                "request_id": response.headers.get("x-request-id") if response else None,
                "client_hash": client_hash(request.client.host if request.client else None),
            },
        )


# Request size limit; the input guard (app/guardrails/input_guard.py) does the rest.
MAX_QUESTION_CHARS = 1000
Award = Literal["MA000009", "MA000004", "MA000003", "MA000002"]

RATE_LIMIT_TEXT = {
    "client_minute": "You're sending questions quickly. Please wait a moment and try again.",
    "client_day": "You've reached today's question limit. Please come back tomorrow.",
    "global_day": "The assistant has reached its daily limit. Please try again tomorrow.",
}


class ChatRequest(BaseModel):
    question: str = Field(min_length=1, max_length=MAX_QUESTION_CHARS)
    award: Award | None = None


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/chat", response_model=ChatResponse)
def chat_endpoint(req: ChatRequest, request: Request, response: Response):
    refusal = limiter.check(request.client.host if request.client else "unknown")
    if refusal:
        log.warning(
            "rate_limited",
            extra={
                "scope": refusal.scope,
                "retry_after_s": refusal.retry_after_s,
                "client_hash": client_hash(request.client.host if request.client else None),
            },
        )
        return JSONResponse(
            status_code=429,
            content={"detail": RATE_LIMIT_TEXT[refusal.scope]},
            headers={"Retry-After": str(refusal.retry_after_s)},
        )
    question = req.question.strip()
    if not question:
        raise HTTPException(status_code=422, detail="Question is empty.")
    try:
        with connect() as conn:
            result = answer_question(conn, question, award=req.award)
    except LLMError:
        raise HTTPException(
            status_code=503,
            detail="The language model is busy right now. Please try again in a minute.",
        ) from None
    # Same id as the Langfuse trace and the guardrail_events rows.
    response.headers["X-Request-ID"] = result.request_id
    return result
