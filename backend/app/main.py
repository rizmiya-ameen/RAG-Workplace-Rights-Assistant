from typing import Literal

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from app.db.conn import connect
from app.llm import LLMError
from app.pipeline import ChatResponse, answer_question

app = FastAPI(
    title="Workplace Rights Assistant",
    description="General information about Australian Modern Awards, the NES and WorkSafe "
    "Victoria guidance. Not legal advice.",
    version="0.1.0",
)

# Basic length limit; the full input guard (PII, injection, advice, scope) is build step 11.
MAX_QUESTION_CHARS = 1000
Award = Literal["MA000009", "MA000004", "MA000003", "MA000002"]


class ChatRequest(BaseModel):
    question: str = Field(min_length=1, max_length=MAX_QUESTION_CHARS)
    award: Award | None = None


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/chat")
def chat_endpoint(req: ChatRequest) -> ChatResponse:
    question = req.question.strip()
    if not question:
        raise HTTPException(status_code=422, detail="Question is empty.")
    try:
        with connect() as conn:
            return answer_question(conn, question, award=req.award)
    except LLMError:
        raise HTTPException(
            status_code=503,
            detail="The language model is busy right now. Please try again in a minute.",
        ) from None
