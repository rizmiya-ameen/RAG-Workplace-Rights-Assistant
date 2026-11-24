"""Context guard: runs on retrieved sources before they go into the prompt (OWASP LLM01,
injection via documents).

1. Domain allow-list: a source is used only if its document URL passes the same check as
   fetching (https, allow-listed host). Ingest already enforces this; checking again here
   means a row added to the database some other way never reaches the model.
2. Clean text: zero-width/control characters and HTML-like tags are removed.
3. Injection scan: a source that contains prompt-injection patterns is dropped.

Wrapping sources as escaped <source> data and masking amounts happen in app/rag/prompts.py.
"""

import re
from dataclasses import dataclass, field, replace

from app.guardrails.input_guard import find_injection
from app.ingestion.fetch import FetchRefused, check_allowed
from app.retrieval.search import Hit

_ZERO_WIDTH = re.compile(r"[\u200b-\u200f\u2060-\u2064\ufeff\u00ad]")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_TAG = re.compile(r"</?[A-Za-z!|][^<>\n]{0,200}>")


@dataclass
class ContextResult:
    hits: list[Hit]
    dropped: dict[str, str] = field(default_factory=dict)  # citation -> reason
    cleaned: int = 0  # sources whose text was changed by cleaning


def url_allowed(url: str) -> bool:
    try:
        check_allowed(url)
    except FetchRefused:
        return False
    return True


def clean_text(text: str) -> str:
    text = _ZERO_WIDTH.sub("", text)
    text = _CONTROL.sub(" ", text)
    return _TAG.sub(" ", text)


def check_context(hits: list[Hit], urls: dict[str, str]) -> ContextResult:
    """urls: source_id -> document URL."""
    result = ContextResult(hits=[])
    for hit in hits:
        if not url_allowed(urls.get(hit.source_id, "")):
            result.dropped[hit.citation] = "domain_not_allowed"
            continue
        content, heading = clean_text(hit.content), clean_text(hit.heading or "")
        if content != hit.content or heading != (hit.heading or ""):
            result.cleaned += 1
            hit = replace(hit, content=content, heading=heading)
        rule = find_injection(f"{heading}\n{content}")
        if rule:
            result.dropped[hit.citation] = f"injection:{rule}"
            continue
        result.hits.append(hit)
    return result
