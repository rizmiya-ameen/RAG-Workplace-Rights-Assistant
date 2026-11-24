"""Output guard: runs on the model's answer before it reaches the user.

Done in app/rag/generate.py (they need the raw reply): canary leak -> blocked, invalid JSON
-> fallback, citations that were not retrieved -> dropped, no valid citation -> fallback.

Here, on the answer text:
1. Dollar amounts: allowed only if they come from the pay tool (`allowed`, router step 14).
   The model never sees amounts (they are masked in the prompt), so any other amount is
   made up and is replaced.
2. PII: emails, phone numbers, TFNs, BSBs and account numbers are redacted.
The disclaimer is added by code in the response, never written by the model.
"""

import re

from app.guardrails.input_guard import redact_pii

AMOUNT_REMOVED = "[amount removed: see the award's pay-rate table]"
_AMOUNT = re.compile(
    r"(?:A?\$|AUD\s?)\s?\d[\d,]*(?:\.\d+)?"
    r"|\b\d[\d,]*(?:\.\d+)?\s?(?:dollars|AUD)\b",
    re.IGNORECASE,
)


def _norm_amount(text: str) -> str:
    digits = re.sub(r"[^\d.]", "", text)
    try:
        return f"{float(digits):.2f}"
    except ValueError:
        return digits


def check_amounts(answer: str, allowed: frozenset[str] = frozenset()) -> tuple[str, int]:
    """Replace dollar amounts that are not in `allowed` (normalised like "27.08")."""
    removed = 0

    def repl(m: re.Match) -> str:
        nonlocal removed
        if _norm_amount(m.group(0)) in allowed:
            return m.group(0)
        removed += 1
        return AMOUNT_REMOVED

    return _AMOUNT.sub(repl, answer), removed


def check_output(answer: str, allowed_amounts: frozenset[str] = frozenset()):
    """Returns (answer, events) where events is a list of (rule, detail)."""
    events: list[tuple[str, dict]] = []
    answer, removed = check_amounts(answer, allowed_amounts)
    if removed:
        events.append(("amount_removed", {"count": removed}))
    answer, pii = redact_pii(answer)
    if pii:
        events.append(("pii_redacted", pii))
    return answer, events
