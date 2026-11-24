import logging
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import FastAPI, HTTPException, Response
from pydantic import BaseModel, Field

from app import observability as obs
from app.db.conn import connect
from app.llm import LLMError
from app.pipeline import ChatResponse, answer_question

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")


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

# Request size limit; the input guard (app/guardrails/input_guard.py) does the rest.
MAX_QUESTION_CHARS = 1000
Award = Literal["MA000009", "MA000004", "MA000003", "MA000002"]


class ChatRequest(BaseModel):
    question: str = Field(min_length=1, max_length=MAX_QUESTION_CHARS)
    award: Award | None = None


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/chat")
def chat_endpoint(req: ChatRequest, response: Response) -> ChatResponse:
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
